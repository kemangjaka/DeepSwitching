"""Deep Switching: A deep learning based camera selection for occlusion-less surgery recording."""

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import argparse
import os
import sys
import pickle
import time
import torch
import numpy as np
sys.path.append(os.getcwd())

from utils import *
from models.DSNet import *
from switching import loss
from switching.utils.DSNet_dataset import Dataset
from switching.utils.DSNet_config import Config
import torch.nn.functional as F

parser = argparse.ArgumentParser()
parser.add_argument('--cfg', default='model_01')
parser.add_argument('--mode', default='train')
parser.add_argument('--data', default='train')
parser.add_argument('--gpu-index', type=int, default=0)
parser.add_argument('--iter', type=int, default=0)
parser.add_argument('--max-iter', type=int, default=4000)
parser.add_argument('--setting', type=int, default=0) # Applied only for sequence-out setting

args = parser.parse_args()
cfg = Config(args.cfg, create_dirs=(args.iter == 0), setting_id=args.setting)

"""setup"""
dtype = torch.float64
torch.set_default_dtype(dtype)
device = torch.device('cuda', index=args.gpu_index) if torch.cuda.is_available() else torch.device('cpu')
if torch.cuda.is_available():
    torch.cuda.set_device(args.gpu_index)
np.random.seed(cfg.seed)
torch.manual_seed(cfg.seed)
tb_logger = Logger(cfg.tb_dir)
logger = create_logger(os.path.join(cfg.log_dir, 'log.txt'))

"""network"""
dsnet = models_func[cfg.network](2, cfg.v_hdim, cfg.cnn_fdim, dtype, device, mlp_dim=cfg.mlp_dim, camera_num=cfg.camera_num, \
        v_net_param=cfg.v_net_param, bi_dir=cfg.bi_dir, training=(args.mode == 'train'), is_dropout=cfg.is_dropout)

if args.iter > 0:
    cp_path = '%s/iter_%04d.p' % (cfg.model_dir, args.iter)
    logger.info('loading model from checkpoint: %s' % cp_path)
    model_cp = pickle.load(open(cp_path, "rb"))
    dsnet.load_state_dict(model_cp['ds_net'], strict=False)

dsnet.to(device)
class_weights = torch.tensor([0.2, 0.8], dtype=dtype, device=device)
cross_entropy_loss = nn.CrossEntropyLoss(weight=class_weights)
cat_crit = nn.NLLLoss(weight=class_weights)
focal_crit = loss.FocalLoss()
switch_crit = loss.SwitchingLoss()
kl_crit = loss.SelectKLLoss()
cont_crit = loss.ContrastiveLoss(margin=1.2, dtype=dtype, device=device)

if cfg.optimizer == 'Adam':
    optimizer = torch.optim.Adam(dsnet.parameters(), lr=cfg.lr, weight_decay=cfg.weightdecay)
else:
    optimizer = torch.optim.SGD(dsnet.parameters(), lr=cfg.lr, weight_decay=cfg.weightdecay)
fr_margin = cfg.fr_margin


logger_str = {'train': "Training: ", 'val': "Validation: "}
_iter = {'train': args.iter, 'val': 0}
if cfg.network == 'DSNet_AR_Cont':
    loss_log = {'train': ['cat_loss','cont_loss', 'acc'], 'val': ['val_cat_loss','val_cont_loss', 'val_acc']}
else:
    loss_log = {'train': ['loss', 'acc'], 'val': ['val_loss', 'val_acc']}

def run_epoch(dataset, mode='train'):
    global dsnet, optimizer, focal_crit, switch_crit, kl_crit, _iter
    """
    img: (B, Cam, S, H, W, Channel)
    labels: (B, Cam, S)
    """
    for imgs_np, labels_np, _ in dataset:
        t0 = time.time()
        imgs = tensor(imgs_np, dtype=dtype, device=device)
        labels = tensor(labels_np, dtype=torch.long, device=device)
        if cfg.network == 'DSNet_AR_Cont':
            prob_pred, pred_features = dsnet(imgs, labels, _iter['train'])
        elif cfg.network == 'dsar' or cfg.network == 'DSNet_ConvAR':
            prob_pred = dsnet(imgs, labels, _iter['train'])
        else:
            prob_pred = dsnet(imgs)
        

        prob_pred = prob_pred[:, :, fr_margin: -fr_margin, :].contiguous()
        labels_gt = labels[:, :, fr_margin:-fr_margin].contiguous()
        if cfg.network == 'DSNet_AR_Cont':
            pred_features = pred_features[:, :, fr_margin:-fr_margin, :]
            cat_loss = focal_crit(prob_pred.view(-1, 2), labels_gt.view(-1,))
            cont_loss = cont_crit(pred_features, labels_gt)
            loss = cat_loss.mean() + 0.25 * cont_loss
        else:
            if cfg.loss == 'cross_entropy':
                cat_loss = cross_entropy_loss(prob_pred.view(-1, 2), labels_gt.view(-1,))
            elif cfg.loss == 'focal':
                cat_loss = focal_crit(prob_pred.view(-1, 2), labels_gt.view(-1,))
            loss = cat_loss.mean()

        if mode == 'train':
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if cfg.save_model_interval > 0 and _iter['train'] % cfg.save_model_interval == 0:
                with to_cpu(dsnet):
                    cp_path = '%s/iter_%04d.p' % (cfg.model_dir, _iter['train'])
                    model_cp = {'ds_net': dsnet.state_dict()}
                    pickle.dump(model_cp, open(cp_path, 'wb'))


        prob_pred = F.softmax(prob_pred, dim=-1).detach().cpu().numpy()[:, :, :, 1]
        select_ind_pred = np.argmax(prob_pred, axis=1)
        label_gt = labels_np[:, :, fr_margin:-fr_margin]
        select_ind_gt = np.argmax(label_gt, axis=1)
        assert select_ind_pred.shape == select_ind_gt.shape, 'shape should match!'
        acc = np.count_nonzero(select_ind_pred == select_ind_gt) / float(select_ind_gt.shape[0] * select_ind_gt.shape[1])

        if cfg.network == 'DSNet_AR_Cont':
            tb_logger.scalar_summary(loss_log[mode], [cat_loss.mean(), cont_loss, acc], _iter[mode])
            logger.info(logger_str[mode] + 'iter {:6d}    time {:.2f}  loss {:.4f}  cat_loss {:.4f} cont_loss {:.4f}    acc {:.4f}'
                        .format(_iter[mode], time.time() - t0, loss, cat_loss.mean(), cont_loss, acc))
        else:
            tb_logger.scalar_summary(loss_log[mode], [loss, acc], _iter[mode])
            logger.info(logger_str[mode] + 'iter {:6d}    time {:.2f}    loss {:.4f} acc {:.4f}'
                            .format(_iter[mode], time.time() - t0, loss, acc))
        _iter[mode]+=1
        
        if _iter['train'] == args.max_iter:
            exit(0)

        """clean up gpu memory"""
        torch.cuda.empty_cache()
        del imgs, labels



if args.mode == 'train':
    to_train(dsnet)

    """Dataset"""
    tr_dataset = Dataset(cfg, 'train', cfg.fr_num, cfg.camera_num, cfg.batch_size, cfg.split, \
        iter_method=cfg.iter_method, shuffle=cfg.shuffle, overlap=2*cfg.fr_margin, \
            num_sample=cfg.num_sample, sub_sample=cfg.sub_sample, setting_id=args.setting)
    
    for _ in range(args.iter // cfg.num_sample, cfg.num_epoch):
        run_epoch(tr_dataset, mode='train')





elif args.mode == 'test':
    to_test(dsnet)
    dataset = Dataset(cfg, 'test', cfg.fr_num,  cfg.camera_num, 1, cfg.split, iter_method='iter', overlap=2*cfg.fr_margin, sub_sample=cfg.sub_sample, setting_id=args.setting)
    print(dataset.takes)
    res_path = '%s/iter_%04d_%s.p' % (cfg.result_dir, args.iter, args.data)
    if os.path.exists(res_path):
        exit(0)
    torch.set_grad_enabled(False)
    res_pred_raw = {}
    res_pred = {}
    res_orig = {}
    take_ = {}
    res_pred_raw_arr = []
    res_pred_arr = []
    res_orig_arr = []
    meta_start_arr = []
    take = dataset.takes[0]
    for imgs_np, labels_np, _ in dataset:
        #print(take)
        if not take in take_:
            take_[take] = dataset.fr_lb + fr_margin
        imgs = tensor(imgs_np, dtype=dtype, device=device).contiguous()
        labels = tensor(labels_np, dtype=torch.long, device=device)
        if cfg.network == 'DSNet_AR_Cont':
            prob_pred, pred_features = dsnet(imgs, labels, 0)
        elif cfg.network == 'dsar' or cfg.network == 'DSNet_ConvAR':
            prob_pred = dsnet(imgs, labels, 0)
        else:
            prob_pred = dsnet(imgs)
        prob_pred = F.softmax(prob_pred[:, :, fr_margin: -fr_margin, :], dim=-1).cpu().numpy()
        select_prob = np.squeeze(prob_pred[:, :, :, 1])
        
        select_ind = np.argmax(select_prob, axis=0) # along camera direction
        res_pred_arr.append(select_ind)

        select_ind_gt = np.argmax(np.squeeze(labels_np[:, :, fr_margin:-fr_margin]), axis=0)
        res_orig_arr.append(select_ind_gt)

        if dataset.cur_ind >= len(dataset.takes) or dataset.takes[dataset.cur_tid] != take:
            if type(select_ind) == np.int64:
                res_pred[take] = np.concatenate(res_pred_arr[:-1])
                res_orig[take] = np.concatenate(res_orig_arr[:-1])
            else:
                res_pred[take] = np.concatenate(res_pred_arr)
                res_orig[take] = np.concatenate(res_orig_arr)
            res_pred_raw_arr, res_pred_arr, res_orig_arr = [], [], []
            take = dataset.takes[dataset.cur_tid]
            take_[take] = dataset.fr_lb + fr_margin

    #results = {'raw_prob': res_pred_raw, 'select_pred': res_pred, 'select_orig': res_orig, 'start_ind': take_}
    results = {'select_pred':res_pred, 'select_orig':res_orig, 'start_ind':take_}
    res_path = '%s/iter_%04d_%s.p' % (cfg.result_dir, args.iter, args.data)
    pickle.dump(results, open(res_path, 'wb'))
    logger.info('saved results to %s' % res_path)
