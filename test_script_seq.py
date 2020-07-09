import os
import numpy as np
import itertools
import subprocess
models = ['model_focal', 'baseline_seq', 'baseline_spac', 'baseline']

_range = np.arange(3000, 4000, 500)

setting = range(5)

args = itertools.product(models, _range.tolist(), setting)
args = list(args)


for i in range(len(args)):
    gpu_id = i % 7
    if gpu_id == 4:
        gpu_id = 7
    cmd = 'python switching/train.py --mode test --data test --cfg ' + args[i][0] + ' --iter ' + str(args[i][1]) + ' --setting ' + str(args[i][2]) + ' --gpu-index ' + str(gpu_id)
    print(cmd)
    os.system(cmd)