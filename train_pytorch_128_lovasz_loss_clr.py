import numpy as np
import pandas as pd
import math
from time import time
from sklearn.model_selection import StratifiedKFold

import torchvision
import torch
import torch.nn as nn
import torch.nn.functional as F

from transform import *
from data_pytorch import TsgDataset
from models.model_pytorch import UNetResNet34_128
from loss_pytorch import lovasz_hinge, dice_bce_loss, RobustFocalLoss2d, mixed_penalty_loss
from metrics import intersection_over_union_thresholds

name = 'unet_resnet34_torch_128'

BATCH_SIZE = 32


def valid_augment(index, image, mask):
    image = do_center_pad_to_factor(image, factor=32)
    mask = (mask >= 0.5).astype(np.float32)
    return index, image, mask


def train_augment(index, image, mask):
    if np.random.rand() < 0.5:
        image, mask = randomHorizontalFlip(image, mask)

    if np.random.rand() < 0.5:
        c = np.random.choice(3)
        if c == 0:
            image, mask = do_random_shift_scale_crop_pad2(image, mask, 0.125)
        if c == 1:
            image, mask = do_elastic_transform2(image, mask, grid=10,
                                                distort=np.random.uniform(0, 0.1))
        if c == 2:
            image, mask = do_shift_scale_rotate2(image, mask, dx=0, dy=0, scale=1,
                                                 angle=np.random.uniform(0, 10))

    if np.random.rand() < 0.5:
        c = np.random.choice(3)
        if c == 0:
            image = do_brightness_shift(image, np.random.uniform(-0.05, +0.05))
        if c == 1:
            image = do_brightness_multiply(image, np.random.uniform(1 - 0.05, 1 + 0.05))
        if c == 2:
            image = do_gamma(image, np.random.uniform(1 - 0.05, 1 + 0.05))

    image, mask = do_center_pad_to_factor2(image, mask, factor=32)
    mask = (mask >= 0.5).astype(np.float32)
    return index, image, mask


def do_eval(net, dataset):
    net.set_mode('eval')

    probs = np.zeros((len(dataset), 101, 101))
    truths = np.zeros((len(dataset), 101, 101))

    for i in range(len(dataset)):
        with torch.no_grad():
            index, image, y_mask, _ = dataset[i]

            hflip_image = np.array(image)[:, ::-1]
            images = np.array([image, hflip_image])
            images = torch.Tensor(images).cuda()

            logit = net(images)
            prob = logit.sigmoid()

            prob = prob.cpu().data.numpy().squeeze()
            mask = prob[0]
            hflip_mask = prob[1][:, ::-1]
            prob = (mask + hflip_mask) / 2
            prob = prob[13:128 - 14, 13:128 - 14]

            probs[i, :, :] = prob
            truths[i, :, :] = y_mask

    iou = intersection_over_union_thresholds(
        np.int32(truths > 0.5), np.int32(probs > 0.5))

    return iou


data = pd.read_csv('../input/data_ids_with_class.csv')
data_ids = data['id'].values
data_class = data['class'].values

kfold = 5
cv = StratifiedKFold(n_splits=kfold, shuffle=True, random_state=1926)

for cv_num, (train_idx, val_idx) in enumerate(cv.split(data_ids, data_class)):
    print('cv:', cv_num)

    f = open('../logs/{}_{}_lovasz_loss_clr.txt'.format(name, cv_num), 'w+')
    f.close()

    train_ids, val_ids = data_ids[train_idx], data_ids[val_idx]

    train_dataset = TsgDataset(root='../input/train', image_ids=train_ids, augment=train_augment, mode='train')
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, num_workers=4)

    valid_dataset = TsgDataset(root='../input/train', image_ids=val_ids, augment=valid_augment, mode='valid')

    net = UNetResNet34_128()
    net.cuda()
    net.load_state_dict(torch.load('../weights/{}_{}_lovasz_loss.th'.format(name, cv_num)))

    max_lr = 1e-4
    min_lr = 1e-6
    optimizer = torch.optim.Adam(net.parameters(), lr=max_lr)

    num_epochs = 800
    circle_size = 50
    num_circles = int(num_epochs / circle_size)
    epoch = 0

    best_iou_metric = np.zeros(num_circles)

    tic = time()

    while epoch < num_epochs:

        train_loss = 0

        epoch_items = len(train_loader)

        for i, (indices, images, y_masks, _) in enumerate(train_loader):
            net.set_mode('train')

            optimizer.zero_grad()

            images = images.cuda()
            y_masks = y_masks.cuda()

            logits = net(images)
            logits = logits.squeeze()

            loss = lovasz_hinge(logits, y_masks)

            loss.backward()
            optimizer.step()

            train_loss += loss.item()

            epoch_ = epoch % circle_size
            lr = min_lr + (max_lr - min_lr) / 2. * (1 + math.cos(
                (epoch_ * epoch_items + i) / (circle_size * epoch_items) * math.pi))

            optimizer = torch.optim.Adam(net.parameters(), lr=lr)

        train_loss = train_loss / len(train_loader)
        eval_iou = do_eval(net, valid_dataset)

        print('[%03d] duration: %.2f train_loss: %.4f valid_iou: %.4f' % (
            epoch + 1, time() - tic, train_loss, eval_iou))
        print('the current learning rate is {}'.format(lr))

        num_circle = int(epoch / circle_size)
        if eval_iou > best_iou_metric[num_circle]:
            best_iou_metric[num_circle] = eval_iou
            print('saving the best model in clr {}'.format(num_circle))
            torch.save(net.state_dict(), '../weights/{}_{}_lovasz_loss_clr_{}.th'.format(name, cv_num, num_circle))

        with open('../logs/{}_{}_lovasz_loss_clr.txt'.format(name, cv_num), 'a+') as f:
            f.write('[%03d] valid_iou: %.4f\n' % (epoch + 1, eval_iou))

        epoch += 1

    net.cpu()
