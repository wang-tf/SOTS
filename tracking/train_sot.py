''' Details
Author: Zhipeng Zhang (zpzhang1995@gmail.com)
Function: read files with [.yaml] [.txt]
Data: 2021.6.23
'''
import _init_paths
import os
import pdb
import wandb
import torch
import socket
import pprint
import argparse
from contextlib import nullcontext
from easydict import EasyDict as edict

import torch.distributed as dist
import utils.read_file as reader
import utils.log_helper as recorder
import utils.model_helper as loader
import utils.lr_scheduler as learner
import utils.sot_builder as builder

from tensorboardX import SummaryWriter
from torch.nn import DataParallel
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader, DistributedSampler
from dataset.siamese_builder import SiameseDataset as data_builder
from core.trainer.siamese_train import siamese_train as trainer

import torch.backends.cudnn as cudnn

eps = 1e-5


def parse_args():
    """
    args for training.
    """
    parser = argparse.ArgumentParser(description='Train Ocean')
    parser.add_argument('--cfg', type=str, default='experiments/AutoMatch.yaml', help='yaml configure file name')
    parser.add_argument('--wandb', action='store_true', help='use wandb to watch training')
    args = parser.parse_args()

    return args


def epoch_train(config, logger, writer_dict, wandb_instance=None):
    # create model
    print('====> build model <====')
    if 'Siam' in config.MODEL.NAME or config.MODEL.NAME in ['Ocean', 'OceanPlus', 'AutoMatch', 'TransT']:
        siambuilder = builder.Siamese_builder(config)
        model = siambuilder.build()
    else:
        raise Exception('Not implemented model type!')

    model = model.cuda()
    logger.info(model)
    model = loader.load_pretrain(model, './pretrain/{0}'.format(config.TRAIN.PRETRAIN), f2b=True, addhead=True)    # load pretrain

    # get optimizer
    if not config.TRAIN.START_EPOCH == config.TRAIN.UNFIX_EPOCH and not config.MODEL.NAME in ['SiamDW', 'SiamFC']:
        optimizer, lr_scheduler = learner.build_siamese_opt_lr(config, model, config.TRAIN.START_EPOCH)
    else:
        if config.MODEL.NAME in ['SiamDW', 'SiamFC']:
            trainable_params = loader.check_trainable(model, logger, print=False)
            optimizer, lr_scheduler = learner.build_simple_siamese_opt_lr(config, trainable_params)
        else:
            optimizer, lr_scheduler = learner.build_siamese_opt_lr(config, model, 0)  # resume wrong (last line)

    # check trainable again
    print('==========check trainable parameters==========')
    trainable_params = loader.check_trainable(model, logger)           # print trainable params info

    # resume or not
    if config.TRAIN.RESUME:   # resume
        model, optimizer, start_epoch, arch = loader.restore_from(model, optimizer, config.TRAIN.RESUME)
    else:
        start_epoch = config.TRAIN.START_EPOCH

    # parallel
    gpus = [int(i) for i in config.COMMON.GPUS.split(',')]
    gpu_num = world_size = len(gpus)  # or use world_size = torch.cuda.device_count()
    gpus = list(range(0, gpu_num))

    logger.info('GPU NUM: {:2d}'.format(len(gpus)))

    device = torch.device('cuda:{}'.format(gpus[0]) if torch.cuda.is_available() else 'cpu')
    if not config.TRAIN.DDP.ISTRUE:
        model = DataParallel(model, device_ids=gpus).to(device)
    else:
        rank = config.TRAIN.DDP.RANK
        model = DistributedDataParallel(model, device_ids=[rank], output_device=rank)

    logger.info(lr_scheduler)
    logger.info('model prepare done')

    if wandb_instance is not None:
        wandb_instance.watch(model)

    for epoch in range(start_epoch, config.TRAIN.END_EPOCH):
        # build dataloader, benefit to tracking
        train_set = data_builder(config)
        if not config.TRAIN.DDP.ISTRUE:
            train_loader = DataLoader(train_set, batch_size=config.TRAIN.BATCH * gpu_num, num_workers=config.TRAIN.WORKERS,
                                      pin_memory=True, sampler=None, drop_last=True)
        else:
            sampler = DistributedSampler(train_set, num_replicas=world_size, rank=rank, shuffle=True, seed=42)
            train_loader = DataLoader(train_set, batch_size=config.TRAIN.BATCH * gpu_num, shuffle=False,
                                      num_workers=config.WORKERS, sampler=sampler, pin_memory=True, drop_last=True)

        # check if it's time to train backbone
        if epoch == config.TRAIN.UNFIX_EPOCH:
            logger.info('training backbone')
            optimizer, lr_scheduler = learner.build_siamese_opt_lr(config, model.module, epoch)
            print('==========double check trainable==========')
            loader.check_trainable(model, logger)  # print trainable params info

        if config.MODEL.NAME in ['SiamFC', 'SiamDW']:
            curLR = lr_scheduler[epoch]
            for param_group in optimizer.param_groups:
                param_group['lr'] = curLR
        else:
            lr_scheduler.step(epoch)
            curLR = lr_scheduler.get_cur_lr()

        inputs = {'data_loader': train_loader, 'model': model, 'optimizer': optimizer, 'device': device,
                  'epoch': epoch + 1, 'cur_lr': curLR, 'config': config,
                    'writer_dict': writer_dict, 'logger': logger, 'wandb_instance': wandb_instance}

        model, writer_dict = trainer(inputs)

        # save model
        loader.save_model(model, epoch, optimizer, config.MODEL.NAME, config, isbest=False)

    writer_dict['writer'].close()


def main():
    # read config
    print('====> load configs <====')
    args = parse_args()
    config = edict(reader.load_yaml(args.cfg))
    os.environ['CUDA_VISIBLE_DEVICES'] = config.COMMON.GPUS
    if config.TRAIN.DDP.ISTRUE:
        dist.init_process_group(backend='nccl', init_method='env://')

    # create logger
    print('====> create logger <====')
    logger, _, tb_log_dir = recorder.create_logger(config, config.MODEL.NAME, 'train')
    # logger.info(pprint.pformat(config))
    logger.info(config)

    # create tensorboard logger
    print('====> create tensorboard <====')
    writer_dict = {
        'writer': SummaryWriter(log_dir=tb_log_dir),
        'train_global_steps': 0,
    }

    if args.wandb:
        logger.info('use wandb to watch training')
        my_hostname = socket.gethostname()
        my_ip = socket.gethostbyname(my_hostname)
        logger.info('Hostname: {}'.format(my_hostname))
        logger.info('IP: {}'.format(my_ip))
        notes = {my_ip: my_hostname}
        # pdb.set_trace()
        wandb_instance = recorder.setup_wandb(config, notes)
        wandb_context = wandb_instance if wandb_instance is not None else nullcontext()

        with wandb_context:
            epoch_train(config, logger, writer_dict, wandb_instance)
    else:
        epoch_train(config, logger, writer_dict, None)


if __name__ == '__main__':
    main()




