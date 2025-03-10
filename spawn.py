import logging
import argparse
import torch
import numpy as np
import wandb
import os
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.multiprocessing as mp
import torch.distributed as dist 
from torch.utils.data.distributed import DistributedSampler
import torchvision
from torchvision import transforms
from torch.utils.data import DataLoader 
from loss import CrossEntropyLoss
import json
from preprocess import backup,update
from Trainer import train, val, save_checkpoint
from model import Model

# fix random seeds for reproducibility
SEED = 123
torch.manual_seed(SEED)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
np.random.seed(SEED)

LOCAL_RANK = int(os.getenv('LOCAL_RANK', -1))  # https://pytorch.org/docs/stable/elastic/run.html
RANK = int(os.getenv('RANK', -1))
WORLD_SIZE = int(os.getenv('WORLD_SIZE', 1))

def select_device(device='', batch_size=0, newline=True):
    device = str(device).strip().lower().replace('cuda:', '')  # to string, 'cuda:0' to '0'
    cpu = device == 'cpu'
    if cpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'  # force torch.cuda.is_available() = False
    elif device:  # non-cpu device requested
        os.environ['CUDA_VISIBLE_DEVICES'] = device  # set environment variable
        assert torch.cuda.is_available(), f'CUDA unavailable, invalid device {device} requested'  # check availability
    cuda = not cpu and torch.cuda.is_available()
    return torch.device('cuda:0' if cuda else 'cpu')

def main(args):
    
    with open(args.config,'r') as f:
        args = json.load(f)
    if args["mode"]["backup"]=="True":
        backup(args)

    # if args["model"]["preprocess"]=="True":
    #     update(args)

    if args["mode"]["Train"]=="True":
        wandb.init(project=args["project"],notes="baseline")
        wandb.config.update(args)
        
    if len(args['gpus'])>1:
        gpu = select_device(args['gpus'],args["batch_size"])
    else:
        gpu = torch.device('cuda:'+args['gpus'])
    
    ngpus_per_node = torch.cuda.device_count()
    
    mp.spawn(main_worker,nprocs=ngpus_per_node,args=(ngpus_per_node,args))

def main_worker(gpu, ngpus_per_node):
    if (ngpus_per_node == 1) or (ngpus_per_node > 1  and gpu == 0):
        logger = logging.getLogger("train")
        logger.setLevel(level=logging.INFO)

    # prepare for (multi-device) GPU training
    if ngpus_per_node > 1:
        dist.init_process_group(backend="nccl" if dist.is_nccl_available() else "gloo",init_method="tcp://127.0.0.1:3333", world_size=ngpus_per_node, rank=gpu)

    transforms_train = transforms.Compose([
    transforms.Resize((args["img_size"], args["img_size"])),
    # transforms.RandomAffine(degrees=90, translate=(.12, .12), scale = (.85, 1.15), shear=.18, fill=255),
    transforms.ToTensor(),

    ])
    transforms_val = transforms.Compose([
    transforms.Resize((args["img_size"], args["img_size"])),
    transforms.ToTensor(),
    ])

    train_set = torchvision.datasets.ImageFolder(root=args["srcDir"]+'train',transform=transforms_train)
    if ngpus_per_node > 1:
        train_sampler = DistributedSampler(train_set,shuffle=True)
    else:
        train_sampler = None
    train_loader = DataLoader(train_set, batch_size=args["batch_size"], num_workers=args["num_workers"], shuffle=False, drop_last=True,sampler=train_sampler)

    # Process 0
    val_set = torchvision.datasets.ImageFolder(root=args["srcDir"]+'val',transform=transforms_val)
    val_loader = DataLoader(val_set, batch_size=args["batch_size"], num_workers=args["num_workers"], shuffle=False, drop_last=True)
    
    # # build model architecture, then print to console
    model = Model(args["model"], num_classes=len(train_set.classes))
    # raise RuntimeError("!!!!")
    if (ngpus_per_node == 1) or (ngpus_per_node > 1  and gpu == 0):
        logger.info("Data Ready!")
        logger.info(model)
        wandb.watch(model)

    if ngpus_per_node > 1:
        model = model.to(gpu)
        model = DDP(model, device_ids=[gpu], output_device=gpu,find_unused_parameters=True)

    # build optimizer, learning rate scheduler. delete every lines containing lr_scheduler for disabling scheduler

    optimizer = torch.optim.SGD(model.parameters(), args["learning_rate"], momentum=0.9, weight_decay=1e-5)
    criterion = CrossEntropyLoss().cuda(gpu)

    best_val_loss = 0
    best_val_acc = 0

    for epoch in range(args["epochs"]):
        metrics_summary = train(train_loader,model,gpu,optimizer,criterion,epoch,args["epochs"])
        metrics_summary.update(val(val_loader,model,criterion,gpu,epoch,args["epochs"]))
        is_best_loss = metrics_summary["val_loss"] < best_val_loss
        is_best_acc = metrics_summary["val_acc"] > best_val_acc
        if (ngpus_per_node == 1) or (ngpus_per_node > 1  and gpu == 0):
            save_checkpoint(
                        {"state_dict": model.state_dict()},
                        epoch = epoch + 1,
                        val_acc = metrics_summary['val_acc'],
                        # "optimizer": optimizer.state_dict(),
                        is_best=is_best_loss,
                        checkpoint=f'{args["saveDir"]}_{len(train_set.classes)}')

            wandb.log({
                            "epoch" : epoch,
                            "acc" : metrics_summary["acc"],
                            "loss" : metrics_summary["loss"],
                            "val_acc" : metrics_summary["val_acc"],
                            "val_loss" : metrics_summary["val_loss"],
                        })
    if (ngpus_per_node == 1) or (ngpus_per_node > 1  and gpu == 0):
        wandb.finish()
        logger.info("Finish")

if __name__ == '__main__':
    args = argparse.ArgumentParser(description='PyTorch Template')
    args.add_argument('-c', '--config', default="config.json", type=str,
                      help='config file path (default: None)')
    
    main(args.parse_args())
