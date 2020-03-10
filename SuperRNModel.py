import os, sys
from argparse import ArgumentParser
from classifier.joints import parse_clip
from classifier.GTheta import get_combinations_inter, get_combinations_intra
from classifier.GTheta import GTheta
from classifier.FPhi import FPhi
from classifier.dataset import PHYTDataset, generatorKFold
import classifier.dataset
import torch.tensor
import torch.nn as nn

# Pytorch Lightning
import pytorch_lightning as pl
from pytorch_lightning import Trainer
from torch.utils.data import DataLoader

import numpy as np
import math

from pytorch_lightning.callbacks import EarlyStopping

def add_model_specific_args(parent_parser, root_dir):
    parser = ArgumentParser(parents=[parent_parser])

    # Specify whether or not to put entire dataset on GPU
    parser.add_argument('--full_gpu', action='store_true')

    # training params (opt)
    parser.add_argument('--patience', default=2, type=int)
    parser.add_argument('--kfold', default=1, type=int)
    parser.add_argument('--epochs', default=10, type=int)
    parser.add_argument('--optim', default='Adam', type=str)
    parser.add_argument('--lr', default=0.0001, type=float)
    parser.add_argument('--momentum', default=0.0, type=float)
    parser.add_argument('--nesterov', action='store_true')
    parser.add_argument('--changeOrder', action='store_true')
    parser.add_argument('--randomJointOrder', default=0, type=int)
    parser.add_argument('--batch_size', default=1, type=int)
    parser.add_argument('--inter', action='store_true')
    parser.add_argument('--intra', action='store_true')
    return parser


class SuperRNModel(pl.LightningModule):
    def __init__(self, hparams):
        super(SuperRNModel, self).__init__()
        self.hparams = hparams
        self.isInter = self.hparams.inter and not self.hparams.intra
        self.isIntra = not self.hparams.inter and self.hparams.intra

        #Load dataset and find its size
        self.dataset = PHYTDataset('datasetCreation/FilteredPoses/',hparams)
        numOfFrames = (len(self.dataset.clips[0][0])-1)//3
        numOfJoints = len(self.dataset.clips[0])//2
        
        if(self.isInter):
            #Init Gmodel
            self.g_model_inter = GTheta(hparams,numOfFrames,numOfJoints)
            #Init FModel
            self.f_model = FPhi(hparams,numOfJoints**2)
        
        if(self.isIntra):
            #Init Gmodel
            self.g_model_intra_P1 = GTheta(hparams,numOfFrames,numOfJoints)
            self.g_model_intra_P2 = GTheta(hparams,numOfFrames,numOfJoints)
            #Init FModel
            sizeFInput = 2 * math.factorial(numOfJoints)//math.factorial(2)//math.factorial(numOfJoints - 2)
            self.f_model = FPhi(hparams,sizeFInput)
        
        self.criterion = nn.BCELoss()


    def forward(self, x):
        # numpy matrix of all combination of inter joints
        p1 = x[:,:int(x.shape[1]/2),:]
        p2 = x[:,int(x.shape[1]/2):,:]
        
        if(self.isInter):
            input_data_clip_combinations = get_combinations_inter(p1, p2)
            tensor_g = self.g_model_inter(input_data_clip_combinations)

            # calculate sum and div
            sum = torch.sum(tensor_g, dim=1)
            size_output_G = tensor_g.shape[1]
            average_output = sum / size_output_G

            tensor_classification = self.f_model(average_output)
            
        if(self.isIntra):
            input_data_clip_combinations_P1 = get_combinations_intra(p1)
            input_data_clip_combinations_P2 = get_combinations_intra(p2)
            tensor_g_P1 = self.g_model_intra_P1(input_data_clip_combinations_P1)
            tensor_g_P2 = self.g_model_intra_P2(input_data_clip_combinations_P2)

            # calculate sum and div
            average_output = torch.empty((0))
            for tensor_g in [tensor_g_P1,tensor_g_P1]:
                sum = torch.sum(tensor_g, dim=1)
                size_output_G = tensor_g.shape[1]
                average_output_temp = sum / size_output_G
                average_output = torch.cat([average_output, average_output_temp], dim=0)
                
            tensor_classification = self.f_model(average_output)
        return tensor_classification

    def configure_optimizers(self):
        if self.hparams.optim == 'Adam':
            return torch.optim.Adam(self.parameters(), lr=self.hparams.lr)
        elif self.hparams.optim == 'SGD':
            return torch.optim.SGD(self.parameters(), lr=self.hparams.lr, momentum=self.hparams.momentum)
        else:
            return None

    def training_step(self, batch, batch_nb):
        # REQUIRED
        x,y = batch
        if self.hparams.full_gpu:
            x = x.cuda()
            y = y.cuda()
        y_hat = self.forward(x)
        loss = self.criterion(y_hat, y.squeeze())
        tensorboard_logs = {'train_loss': loss}
        return {'loss': loss, 'log': tensorboard_logs}

    @pl.data_loader
    def train_dataloader(self):
        # REQUIRED
        return DataLoader(self.dataset, batch_size=self.hparams.batch_size, shuffle=True)
    
    @pl.data_loader
    def test_dataloader(self):
        self.testResults = []
        class temp():
            def __init__(self,x,y):
                self.x = x
                self.y = y
            
            def __len__(self):
                return len(self.x)
            def __getitem__(self, index):
                return self.x[index], self.y[index]
        return DataLoader(temp(self.dataset.testclips,self.dataset.testy), batch_size=self.hparams.batch_size, shuffle=True)
        # return DataLoader(temp(self.dataset.clips,self.dataset.y), batch_size=self.hparams.batch_size, shuffle=True)
    
    def test_step(self,batch,batch_idx):
        x,y = batch
        y_hat = self.forward(x)
        y_hat_rounded = torch.round(y_hat)
        #print(y_hat_rounded,y, (y_hat_rounded == y).tolist()[0][0])
        self.testResults.append((y_hat_rounded == y).tolist()[0][0])
        return {'test_loss':self.criterion(y_hat,y.squeeze())}
    def test_end(self, outputs):
        avg_loss = torch.stack([x['test_loss'] for x in outputs]).mean()
        return {'test_loss': avg_loss}
    
if __name__ == '__main__':
    # use default args given by lightning
    root_dir = os.path.split(os.path.dirname(sys.modules['__main__'].__file__))[0]
    parent_parser = ArgumentParser(add_help=False)
    
    # allow model to overwrite or extend args
    parser = add_model_specific_args(parent_parser, root_dir)
    hyperparams = parser.parse_args()
    
    #Check to see if inter or intra or inter+intra
    if(not hyperparams.inter and not hyperparams.intra):
        class noTypeSelected(Exception):
            def __init__(self,s):
                pass
        raise noTypeSelected("Must select at least one: --inter, --intra")
            
    accuList = []
    classifier.dataset.KFoldLength = hyperparams.kfold
    for i in range(classifier.dataset.KFoldLength):
        #Set Seeds
        torch.manual_seed(0)
        np.random.seed(0)
            
        rnModel = SuperRNModel(hyperparams)

        early_stop_callback = EarlyStopping(
            monitor='loss',
            min_delta=0.2,
            patience=hyperparams.patience,
            verbose=False,
            mode='max'
        )

        trainer = Trainer(max_nb_epochs=hyperparams.epochs, early_stop_callback=early_stop_callback, checkpoint_callback=None)


        trainer.fit(rnModel)
        trainer.test()
        
        results = rnModel.testResults
        print('KFold %d/%d: Accuracy = '%(i,classifier.dataset.KFoldLength),sum(results)/len(results))

    print('Done!')
    print('Global Accuracy:',sum(accuList)/len(accuList))




