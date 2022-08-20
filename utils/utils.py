import torch
import numpy as np
import random
import time
import os
from pathlib import Path
from torch.backends import cudnn
from torch import nn, Tensor
from torch.autograd import profiler
from typing import Union
from torch import distributed as dist


def fix_seeds(seed: int = 123) -> None:
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

def setup_cudnn() -> None:
    # Speed-reproducibility tradeoff https://pytorch.org/docs/stable/notes/randomness.html
    cudnn.benchmark = True
    cudnn.deterministic = False

def time_sync() -> float:
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    return time.time()

def get_model_size(model: Union[nn.Module, torch.jit.ScriptModule]):
    tmp_model_path = Path('temp.p')
    if isinstance(model, torch.jit.ScriptModule):
        torch.jit.save(model, tmp_model_path)
    else:
        torch.save(model.state_dict(), tmp_model_path)
    size = tmp_model_path.stat().st_size
    os.remove(tmp_model_path)
    return size / 1e6   # in MB

@torch.no_grad()
def test_model_latency(model: nn.Module, inputs: torch.Tensor, use_cuda: bool = False) -> float:
    with profiler.profile(use_cuda=use_cuda) as prof:
        _ = model(inputs)
    return prof.self_cpu_time_total / 1000  # ms

def count_parameters(model: nn.Module) -> float:
    return sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6      # in M

def setup_ddp() -> None:
    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ:
        rank = int(os.environ['RANK'])
        world_size = int(os.environ['WORLD_SIZE'])
        gpu = int(os.environ(['LOCAL_RANK']))
        torch.cuda.set_device(gpu)
        dist.init_process_group('nccl', init_method="env://",world_size=world_size, rank=rank)
        dist.barrier()
    else:
        gpu = 0
    return gpu

def cleanup_ddp():
    if dist.is_initialized():
        dist.destroy_process_group()

def reduce_tensor(tensor: Tensor) -> Tensor:
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.ReduceOp.SUM)
    rt /= dist.get_world_size()
    return rt

@torch.no_grad()
def throughput(dataloader, model: nn.Module, times: int = 30):
    model.eval()
    images, _  = next(iter(dataloader))
    images = images.cuda(non_blocking=True)
    B = images.shape[0]
    print(f"Throughput averaged with {times} times")
    start = time_sync()
    for _ in range(times):
        model(images)
    end = time_sync()

    print(f"Batch Size {B} throughput {times * B / (end - start)} images/s")

def get_params_flops(model: nn.Module, size: tuple):
    flops, params = 0.0, 0.0
    try:
        from mmcv.cnn.utils import get__model_complexity_info
        flops, params = get__model_complexity_info(model, input_shape=(3, *size))
    except:
        print('Install mmcv to get FLOPs. \n`pip install mmcv`')
    print(f"Params (M): {params}")
    print(f"GFLOPs (B): {flops}")