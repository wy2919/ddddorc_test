import json
import os
import random
import time

import tqdm

from configs import Config
from loguru import logger
from utils import load_cache
from nets import Net


class Train:
    def __init__(self, project_name: str):
        self.project_name = project_name
        self.project_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects",
                                         project_name)
        self.checkpoints_path = os.path.join(self.project_path, "checkpoints")
        self.models_path = os.path.join(self.project_path, "models")
        self.epoch = 0
        self.step = 0
        self.lr = None
        self.state_dict = None
        self.optimizer = None
        self.config = Config(project_name)
        self.conf = self.config.load_config()

        self.test_step = self.conf['Train']['TEST_STEP']
        self.save_checkpoints_step = self.conf['Train']['SAVE_CHECKPOINTS_STEP']

        self.target = self.conf['Train']['TARGET']
        self.target_acc = self.target['Accuracy']
        self.min_epoch = self.target['Epoch']
        self.max_loss = self.target['Cost']

        self.resize = [int(self.conf['Model']['ImageWidth']), int(self.conf['Model']['ImageHeight'])]
        self.word = self.conf['Model']['Word']
        self.ImageChannel = self.conf['Model']['ImageChannel']
        logger.info("\nTaget:\nmin_Accuracy: {}\nmin_Epoch: {}\nmax_Loss: {}".format(self.target_acc, self.min_epoch,
                                                                                     self.max_loss))
        self.use_gpu = self.conf['System']['GPU']
        if self.use_gpu:
            self.gpu_id = self.conf['System']['GPU_ID']
            logger.info("\nUSE GPU ----> {}".format(self.gpu_id))
            self.device = Net.get_device(self.gpu_id)

        else:
            self.gpu_id = -1
            self.device = Net.get_device(self.gpu_id)
            logger.info("\nUSE CPU".format(self.gpu_id))
        logger.info("\nSearch for history checkpoints...")
        history_checkpoints = os.listdir(self.checkpoints_path)
        if len(history_checkpoints) > 0:
            history_step = 0
            newer_checkpoint = None
            for checkpoint in history_checkpoints:
                checkpoint_name = checkpoint.split(".")[0].split("_")
                if int(checkpoint_name[3]) > history_step:
                    newer_checkpoint = checkpoint
                    history_step = int(checkpoint_name[3])
            param, self.state_dict, self.optimizer= Net.load_checkpoint(
                os.path.join(self.checkpoints_path, newer_checkpoint), self.device)
            self.epoch, self.step, self.lr = param['epoch'], param['step'], param['lr']
            self.epoch += 1
            self.step += 1

        else:
            logger.info("\nEmpty history checkpoints")

        logger.info("\nBuilding Net...")
        self.net = Net(self.conf, self.lr)
        if self.state_dict:
            self.net.load_state_dict(self.state_dict)
        logger.info(self.net)
        logger.info("\nBuilding End")



        self.net = self.net.to(self.device)
        logger.info("\nGet Data Loader...")

        loaders = load_cache.GetLoader(project_name)
        self.train = loaders.loaders['train']
        self.val = loaders.loaders['val']
        del loaders
        logger.info("\nGet Data Loader End!")

        self.loss = 0
        self.avg_loss = 0
        self.start_time = time.time()
        self.now_time = time.time()

    def start(self):
        val_iter = iter(self.val)
        while True:
            for idx, (inputs, labels, labels_length) in enumerate(self.train):
                self.now_time = time.time()
                inputs = self.net.variable_to_device(inputs, device=self.device)

                loss, lr = self.net.trainer(inputs, labels, labels_length)

                self.avg_loss += loss

                self.step += 1

                if self.step % 100 == 0 and self.step % self.test_step != 0:
                    logger.info("{}\tEpoch: {}\tStep: {}\tLastLoss: {}\tAvgLoss: {}\tLr: {}".format(
                        time.strftime("[%Y-%m-%d-%H_%M_%S]", time.localtime(self.now_time)), self.epoch, self.step,
                        str(loss), str(self.avg_loss / 100), lr
                    ))
                    self.avg_loss = 0
                if self.step % self.save_checkpoints_step == 0 and self.step != 0:
                    model_path = os.path.join(self.checkpoints_path, "checkpoint_{}_{}_{}.tar".format(
                        self.project_name, self.epoch, self.step,
                    ))
                    self.net.scheduler.step()
                    self.net.save_model(model_path,
                                        {"net": self.net.state_dict(), "optimizer": self.net.optimizer.state_dict(),
                                         "epoch": self.epoch, "step": self.step, "lr": lr})

                if self.step % self.test_step == 0:
                    try:
                        test_inputs, test_labels, test_labels_length = next(val_iter)
                    except Exception:
                        del val_iter
                        val_iter = iter(self.val)
                        test_inputs, test_labels, test_labels_length = next(val_iter)
                    if test_inputs.shape[0] < 5:
                        continue
                    test_inputs = self.net.variable_to_device(test_inputs, self.device)
                    self.net = self.net.train(False)
                    pred_labels, labels_list, correct_list, error_list = self.net.tester(test_inputs, test_labels,
                                                                                         test_labels_length)
                    self.net = self.net.train()
                    accuracy = len(correct_list) / test_inputs.shape[0]
                    logger.info("{}\tEpoch: {}\tStep: {}\tLastLoss: {}\tAvgLoss: {}\tLr: {}\tAcc: {}".format(
                        time.strftime("[%Y-%m-%d-%H_%M_%S]", time.localtime(self.now_time)), self.epoch, self.step,
                        str(loss), str(self.avg_loss / 100), lr, accuracy
                    ))
                    self.avg_loss = 0
                    if accuracy > self.target_acc and self.epoch > self.min_epoch and self.avg_loss < self.max_loss:
                        logger.info("\nTraining Finished!Exporting Model...")
                        dummy_input = self.net.get_random_tensor()
                        input_names = ["input1"]
                        output_names = ["output"]

                        if self.net.backbone.startswith("effnet"):
                            self.net.cnn.set_swish(memory_efficient=False)
                        self.net = self.net.eval().cpu()
                        dynamic_ax = {'input1': {3: 'image_wdith'}, "output": {1: 'seq'}}
                        self.net.export_onnx(self.net, dummy_input,
                                             os.path.join(self.models_path, "{}_{}_{}_{}_{}.onnx".format(
                                                 self.project_name, str(accuracy), self.epoch, self.step,
                                                 time.strftime("%Y-%m-%d-%H-%M-%S", time.localtime(self.now_time))))
                                             , input_names, output_names, dynamic_ax)
                        with open(os.path.join(self.models_path, "charsets.json"), 'w', encoding="utf-8") as f:
                            f.write(json.dumps({"charset": self.net.charset, "image": self.resize, "word": self.word, 'channel': self.ImageChannel}, ensure_ascii=False))
                        logger.info("\nExport Finished!Using Time: {}min".format(
                            str(int(int(self.now_time) - int(self.start_time)) / 60)))
                        exit()

            self.epoch += 1


if __name__ == '__main__':
    Train("test1")
