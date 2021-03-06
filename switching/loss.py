import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.autograd import Function
import numpy as np
from torch.autograd import Variable

def batched_cdist_l2(x1, x2):
    x1_norm = x1.pow(2).sum(dim=-1, keepdim=True)
    x2_norm = x2.pow(2).sum(dim=-1, keepdim=True)
    res = torch.baddbmm(
        x2_norm.transpose(-2, -1),
        x1,
        x2.transpose(-2, -1),
        alpha=-2
    ).add_(x1_norm).clamp_min_(1e-30).sqrt_()
    return res

def pairwise_distances(x, y=None):
    '''
    Input: x is a Nxd matrix
           y is an optional Mxd matirx
    Output: dist is a NxM matrix where dist[i,j] is the square norm between x[i,:] and y[j,:]
            if y is not given then use 'y=x'.
    i.e. dist[i,j] = ||x[i,:]-y[j,:]||^2
    '''
    x_norm = (x**2).sum(1).view(-1, 1)
    if y is not None:
        y_norm = (y**2).sum(1).view(1, -1)
    else:
        y = x
        y_norm = x_norm.view(1, -1)

    dist = x_norm + y_norm - 2.0 * torch.mm(x, torch.transpose(y, 0, 1))
    return dist

class ContrastiveLoss(nn.Module):
    def __init__(self, margin, dtype, device, alpha=10.0):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin
        self.alpha=alpha
        self.dtype=dtype
        self.device=device
        
    #pred_features: batch, cameraNum, frameNum, feat_dim
    #target: batch, cameraNum, frameNum
    def forward(self, pred_features, target):
        pred_features = pred_features.permute(0, 2, 1, 3).contiguous()
        target = target.permute(0, 2, 1).contiguous()
        batch_size, fr_num, cam_size, feat_size = pred_features.size()
        mask = torch.eye(cam_size, cam_size, dtype=self.dtype, device=self.device).bool()

        x = pred_features.view(-1, cam_size, feat_size)
        dist_mat = torch.cdist(x, x, p=2)
        select_ind = target.view(-1, cam_size).nonzero()[:, 1]
        target_matrix = torch.ones([batch_size * fr_num, cam_size, cam_size], dtype=self.dtype, device=self.device)
        target_matrix[:, select_ind, :] = 0
        target_matrix[:, :, select_ind] = 0
        loss = target_matrix * dist_mat + self.alpha * (1 - target_matrix) * F.relu(self.margin - dist_mat)
        loss.masked_fill_(mask, 0)
        loss = loss[:, torch.triu(torch.ones(cam_size, cam_size)) == 1]
        return loss.mean()
        '''
        print(loss)
        _loss = loss.view(-1, fr_num, cam_size, cam_size)


        
        for i in range(fr_num):
            x = pred_features[:, i, :, :].view(-1, cam_size, feat_size)
            dist_mat = torch.cdist(x, x, p=2)
            select_ind = target[:, i, :].nonzero()[:, 1]
            print(select_ind)
            target_matrix = torch.ones([batch_size, cam_size, cam_size], dtype=self.dtype, device=self.device)
            target_matrix[:, select_ind, :] = 0
            target_matrix[:, :, select_ind] = 0
            print(target_matrix)

            loss = target_matrix * dist_mat + self.alpha * (1 - target_matrix) * F.relu(self.margin - dist_mat)
            loss.masked_fill_(mask, 0)

            #loss = loss.view(-1, cam_size * cam_size)


            print(loss)
            print(_loss[:, i, :, :])
        '''
        
            


class FocalLoss(nn.Module):
    
    def __init__(self, weight=None, 
                 gamma=2., reduction='none'):
        nn.Module.__init__(self)
        self.weight = weight
        self.gamma = gamma
        self.reduction = reduction
        
    def forward(self, input_tensor, target_tensor):
        log_prob = F.log_softmax(input_tensor, dim=-1)
        prob = torch.exp(log_prob)
        return F.nll_loss(
            ((1 - prob) ** self.gamma) * log_prob, 
            target_tensor, 
            weight=self.weight,
            reduction = self.reduction
        )
'''
#https://github.com/DingKe/pytorch_workplace/blob/master/focalloss/loss.py
class FocalLoss(nn.Module):

    def __init__(self, dtype, device, gamma=0, eps=1e-7):
        super(FocalLoss, self).__init__()
        self.dtype=dtype
        self.device=device
        self.gamma = gamma
        self.eps = eps

    def one_hot(self, index, classes):
        size = index.size() + (classes,)
        view = index.size() + (1,)

        mask = torch.Tensor(*size, dtype=self.dtype, device=self.device).fill_(0)
        index = index.view(*view)
        ones = 1.

        if isinstance(index, Variable):
            ones = Variable(torch.Tensor(index.size(), dtype=self.dtype, device=self.device).fill_(1))
            mask = Variable(mask, volatile=index.volatile)
        return mask.scatter_(1, index, ones)


    def forward(self, input, target):
        y = self.one_hot(target, input.size(-1))
        input = input.clamp(self.eps, 1. - self.eps)

        loss = -1 * y * torch.log(input) # cross entropy
        loss = loss * (1 - input) ** self.gamma # focal loss
        print(loss.size())
        return loss.sum()
'''
class SelectKLLoss(nn.Module):
    def __init__(self, eps=1e-8):
        super(SelectKLLoss, self).__init__()
        self.eps=eps
        self.logsoftmax = nn.LogSoftmax(dim=1)
        self.softmax = nn.Softmax(dim=1)
        self.kldiv = nn.KLDivLoss(reduction='none')
        self.switch_weight = 0.9

    def forward(self, select_probs, gt_switch):
        seq_num = select_probs.size()[1]
        cam_num = select_probs.size()[2]
        prev_prob = select_probs[:, :-1, :].contiguous().view(-1, cam_num)
        next_prob = select_probs[:, 1: , :].contiguous().view(-1, cam_num)
        kl_div = self.kldiv(self.logsoftmax(next_prob), self.softmax(prev_prob)).sum(dim=1).view(-1, seq_num - 1)
        switch_loss = (1.0 - self.switch_weight) * (1.0 - gt_switch) * kl_div - self.switch_weight * gt_switch * kl_div
        switch_loss = switch_loss ** 2
        return switch_loss.sum(dim=1)


class SwitchingLoss(nn.Module):
    def __init__(self, eps=1e-8):
        super(SwitchingLoss, self).__init__()
        self.eps=eps

    def forward(self, preds_indices, gt_switch):
        prev_indices_pred = preds_indices[:, :-1]
        next_indices_pred = preds_indices[:, 1: ]
        diff = torch.abs(next_indices_pred - prev_indices_pred)
        switched = diff / (diff + self.eps)
        switch_loss = gt_switch * (switched - 1.0) ** 2 + \
            (1.0 - gt_switch) * switched ** 2
        return switch_loss.sum(dim=1)

if __name__ == "__main__":
    device = torch.device("cuda")
    '''
    focal_without_onehot = FocalLossWithOutOneHot(gamma=1)
    focal_with_onehot = FocalLossWithOneHot(gamma=1)
    input = torch.Tensor([[0.3, 0.1, 0.1], [0.3, 0.6, 0.001], [0.01, 0.002, 2.3], [0.01, 0.002, 2.3]]).to(device)
    target = torch.Tensor([0, 1, 1, 2]).long().to(device)

    focal_without_onehot(input, target)
    # exception will occur when input and target are stored to GPU(s).
    focal_with_onehot(input, target)
    '''

    switch_loss = SwitchingLoss()
    #gt_switch = torch.Tensor([0, 0, 0, 0], [])
    _indices = torch.Tensor([[1, 1, 1, 1, 1], [1, 1, 1, 1, 2], [1, 1, 2, 1, 1], [1, 1, 2, 1, 2], [1, 2, 1, 3, 1]])
    print(switch_loss(_indices))
