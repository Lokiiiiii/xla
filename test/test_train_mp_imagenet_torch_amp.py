import test.args_parse as args_parse

SUPPORTED_MODELS = [
    "alexnet",
    "densenet121",
    "densenet161",
    "densenet169",
    "densenet201",
    "inception_v3",
    "resnet101",
    "resnet152",
    "resnet18",
    "resnet34",
    "resnet50",
    "squeezenet1_0",
    "squeezenet1_1",
    "vgg11",
    "vgg11_bn",
    "vgg13",
    "vgg13_bn",
    "vgg16",
    "vgg16_bn",
    "vgg19",
    "vgg19_bn",
]

MODEL_OPTS = {
    "--model": {
        "choices": SUPPORTED_MODELS,
        "default": "resnet50",
    },
    "--test_set_batch_size": {
        "type": int,
    },
    "--lr_scheduler_type": {
        "type": str,
    },
    "--lr_scheduler_divide_every_n_epochs": {
        "type": int,
    },
    "--lr_scheduler_divisor": {
        "type": int,
    },
}

FLAGS = args_parse.parse_common_options(
    datadir="/tmp/imagenet",
    batch_size=None,
    num_epochs=None,
    momentum=None,
    lr=None,
    target_accuracy=None,
    profiler_port=9012,
    opts=MODEL_OPTS.items(),
)

import os
import time
import test.schedulers as schedulers
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import torch_xla.utils.utils as xu
import torch_xla.test.test_utils as test_utils
import torch_xla.core.xla_model as xm
from test.classification_benchmark_constants import DEFAULT_KWARGS, MODEL_SPECIFIC_DEFAULTS
from torch.cuda.amp import GradScaler, autocast

default_value_dict = MODEL_SPECIFIC_DEFAULTS.get(FLAGS.model, DEFAULT_KWARGS)
for arg, value in default_value_dict.items():
    if getattr(FLAGS, arg) is None:
        setattr(FLAGS, arg, value)


def get_model_property(key):
    default_model_property = {"img_dim": 224, "model_fn": getattr(torchvision.models, FLAGS.model)}
    model_properties = {
        "inception_v3": {
            "img_dim": 299,
            "model_fn": lambda: torchvision.models.inception_v3(aux_logits=False),
        },
    }
    model_fn = model_properties.get(FLAGS.model, default_model_property)[key]
    return model_fn


def _train_update(device, step, loss, tracker, epoch, writer):
    test_utils.print_training_update(
        device,
        step,
        loss.item(),
        tracker.rate(),
        tracker.global_rate(),
        epoch,
        summary_writer=writer,
    )


def train_imagenet():
    print("==> Preparing data..")
    img_dim = get_model_property("img_dim")
    if FLAGS.fake_data:
        train_dataset_len = 1200000  # Roughly the size of Imagenet dataset.
        # train_loader = xu.SampleGenerator(
        #     data=(
        #         torch.zeros(FLAGS.batch_size, 3, img_dim, img_dim),
        #         torch.zeros(FLAGS.batch_size, dtype=torch.int64),
        #     ),
        #     sample_count=train_dataset_len // FLAGS.batch_size // xm.xrt_world_size(),
        # )
        train_loader = xu.SampleGenerator(
            data=(
                torch.zeros(FLAGS.batch_size, 3, img_dim, img_dim),
                torch.zeros(FLAGS.batch_size, dtype=torch.int64),
            ),
            sample_count=train_dataset_len // FLAGS.batch_size,
        )
        # test_loader = xu.SampleGenerator(
        #     data=(
        #         torch.zeros(FLAGS.test_set_batch_size, 3, img_dim, img_dim),
        #         torch.zeros(FLAGS.test_set_batch_size, dtype=torch.int64),
        #     ),
        #     sample_count=50000 // FLAGS.batch_size // xm.xrt_world_size(),
        # )
        test_loader = xu.SampleGenerator(
            data=(
                torch.zeros(FLAGS.test_set_batch_size, 3, img_dim, img_dim),
                torch.zeros(FLAGS.test_set_batch_size, dtype=torch.int64),
            ),
            sample_count=50000 // FLAGS.batch_size,
        )
    else:
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        train_dataset = torchvision.datasets.ImageFolder(
            os.path.join(FLAGS.datadir, "train"),
            transforms.Compose(
                [
                    transforms.RandomResizedCrop(img_dim),
                    transforms.RandomHorizontalFlip(),
                    transforms.ToTensor(),
                    normalize,
                ]
            ),
        )
        train_dataset_len = len(train_dataset.imgs)
        resize_dim = max(img_dim, 256)
        test_dataset = torchvision.datasets.ImageFolder(
            os.path.join(FLAGS.datadir, "val"),
            # Matches Torchvision's eval transforms except Torchvision uses size
            # 256 resize for all models both here and in the train loader. Their
            # version crashes during training on 299x299 images, e.g. inception.
            transforms.Compose(
                [
                    transforms.Resize(resize_dim),
                    transforms.CenterCrop(img_dim),
                    transforms.ToTensor(),
                    normalize,
                ]
            ),
        )

        train_sampler, test_sampler = None, None
        # if xm.xrt_world_size() > 1:
        #     train_sampler = torch.utils.data.distributed.DistributedSampler(
        #         train_dataset, num_replicas=xm.xrt_world_size(), rank=xm.get_ordinal(), shuffle=True
        #     )
        #     test_sampler = torch.utils.data.distributed.DistributedSampler(
        #         test_dataset, num_replicas=xm.xrt_world_size(), rank=xm.get_ordinal(), shuffle=False
        #     )
        train_loader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=FLAGS.batch_size,
            sampler=train_sampler,
            drop_last=FLAGS.drop_last,
            shuffle=False if train_sampler else True,
            num_workers=FLAGS.num_workers,
        )
        test_loader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=FLAGS.test_set_batch_size,
            sampler=test_sampler,
            drop_last=FLAGS.drop_last,
            shuffle=False,
            num_workers=FLAGS.num_workers,
        )

    torch.manual_seed(42)

    # device = xm.xla_device()
    device = torch.device("cuda")  # Default CUDA device
    model = get_model_property("model_fn")().to(device)
    writer = None
    # if xm.is_master_ordinal():
    #     writer = test_utils.get_summary_writer(FLAGS.logdir)
    optimizer = optim.SGD(
        model.parameters(), lr=FLAGS.lr, momentum=FLAGS.momentum, weight_decay=1e-4
    )
    # num_training_steps_per_epoch = train_dataset_len // (FLAGS.batch_size * xm.xrt_world_size())
    num_training_steps_per_epoch = train_dataset_len // (FLAGS.batch_size * 1)
    lr_scheduler = schedulers.wrap_optimizer_with_scheduler(
        optimizer,
        scheduler_type=getattr(FLAGS, "lr_scheduler_type", None),
        scheduler_divisor=getattr(FLAGS, "lr_scheduler_divisor", None),
        scheduler_divide_every_n_epochs=getattr(FLAGS, "lr_scheduler_divide_every_n_epochs", None),
        num_steps_per_epoch=num_training_steps_per_epoch,
        summary_writer=writer,
    )
    loss_fn = nn.CrossEntropyLoss()
    scaler = GradScaler()

    def train_loop_fn(loader, epoch):
        tracker = xm.RateTracker()
        model.train()
        for step, (data, target) in enumerate(loader):
            data = data.to(device)
            target = target.to(device)
            optimizer.zero_grad()
            with autocast():
                output = model(data)
                loss = loss_fn(output, target)

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            # optimizer.step()
            # xm.optimizer_step(optimizer)
            tracker.add(FLAGS.batch_size)
            if lr_scheduler:
                lr_scheduler.step()
            if step % FLAGS.log_steps == 0:
                _train_update(device, step, loss, tracker, epoch, writer)
                # xm.add_step_closure(
                #     _train_update, args=(device, step, loss, tracker, epoch, writer)
                # )

    def test_loop_fn(loader, epoch):
        total_samples, correct = 0, 0
        model.eval()
        for step, (data, target) in enumerate(loader):
            output = model(data)
            pred = output.max(1, keepdim=True)[1]
            correct += pred.eq(target.view_as(pred)).sum()
            total_samples += data.size()[0]
            if step % FLAGS.log_steps == 0:
                test_utils.print_test_update(device, None, epoch, step)
                # xm.add_step_closure(test_utils.print_test_update, args=(device, None, epoch, step))
        accuracy = 100.0 * correct.item() / total_samples
        # accuracy = xm.mesh_reduce("test_accuracy", accuracy, np.mean)
        return accuracy

    # train_device_loader = pl.MpDeviceLoader(train_loader, device)
    # test_device_loader = pl.MpDeviceLoader(test_loader, device)
    accuracy, max_accuracy = 0.0, 0.0
    for epoch in range(1, FLAGS.num_epochs + 1):
        print("Epoch {} train begin {}".format(epoch, test_utils.now()))
        train_loop_fn(train_loader, epoch)
        print("Epoch {} train end {}".format(epoch, test_utils.now()))
        accuracy = test_loop_fn(test_loader, epoch)
        print("Epoch {} test end {}, Accuracy={:.2f}".format(epoch, test_utils.now(), accuracy))
        max_accuracy = max(accuracy, max_accuracy)
        test_utils.write_to_summary(
            writer, epoch, dict_to_write={"Accuracy/test": accuracy}, write_xla_metrics=True
        )
        # if FLAGS.metrics_debug:
        #     print(met.metrics_report())

    test_utils.close_summary_writer(writer)
    print("Max Accuracy: {:.2f}%".format(max_accuracy))
    return max_accuracy


def _mp_fn(flags):
    global FLAGS
    FLAGS = flags
    torch.set_default_tensor_type("torch.FloatTensor")
    accuracy = train_imagenet()
    if accuracy < FLAGS.target_accuracy:
        print("Accuracy {} is below target {}".format(accuracy, FLAGS.target_accuracy))
        sys.exit(21)


if __name__ == "__main__":
    _mp_fn(FLAGS)
    # xmp.spawn(_mp_fn, args=(FLAGS,), nprocs=FLAGS.num_cores)
