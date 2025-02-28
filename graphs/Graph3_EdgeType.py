#          Graph 3 Edge Type          #
# ------------------------------------#
# Node Features: Speeds from Past Hour
# Edges Included: Type 1, Type 2, Type 3
# Edge Types: Learned

import torch
import numpy as np
import pandas as pd
from torch_geometric.loader import DataLoader
from torch_geometric.data import InMemoryDataset, Data
import torch.optim as optim
from tqdm import tqdm
import time
import os
import matplotlib.pyplot as plt
from torch.utils.tensorboard import SummaryWriter
import torch.nn.functional as F
from torch_geometric.nn import GATv2Conv, HeteroConv
from datetime import datetime
from torch_geometric.data import HeteroData

###### Load in datasets ######

current_script_directory = os.path.dirname(os.path.abspath(__file__))

vds_info_path = os.path.join(current_script_directory, '..', 'data', 'vds_info_w_lanes.csv')
sensor_speed_path = os.path.join(current_script_directory, '..', 'data', 'sensor_speed.csv')
sensor_dist_path = os.path.join(current_script_directory, '..', 'data', 'sensor_dist.csv')
sensor_conn_path = os.path.join(current_script_directory, '..', 'data', 'sensor_conn.csv')
non_conn_path = os.path.join(current_script_directory, '..', 'data', 'non_conn.csv')

vds_info = pd.read_csv(vds_info_path).set_index('vds_id')
sensor_speed = pd.read_csv(sensor_speed_path).set_index('vds_id')
sensor_dist = pd.read_csv(sensor_dist_path).set_index('Unnamed: 0')
sensor_conn = pd.read_csv(sensor_conn_path).set_index('Unnamed: 0')
non_conn = pd.read_csv(non_conn_path).set_index('Unnamed: 0')

###### Functions for Model Evaluation ######

def z_score(x, mean, std):
    return (x - mean) / std

def un_z_score(x_normed, mean, std):
    return x_normed * std  + mean

def MAPE(v, v_):
    return torch.mean(torch.abs((v_ - v)) /(v + 1e-15) * 100)

def RMSE(v, v_):
    return torch.sqrt(torch.mean((v_ - v) ** 2))

def MAE(v, v_):
    return torch.mean(torch.abs(v_ - v))

def get_splits(dataset, n_slot, splits):
    split_train, split_val, split_test = splits
    i = n_slot*split_train
    j = n_slot*split_val
    train = dataset[:i]
    val = dataset[i:i+j]
    test = dataset[i+j:]

    return train, val, test

@torch.no_grad()
def eval(model, device, dataloader, type=''):
    model.eval()
    model.to(device)

    mae = 0
    rmse = 0
    mape = 0
    n = 0

    # Evaluate model on all data
    for i, batch in enumerate(dataloader):
        batch = batch.to(device)
        if batch.x_dict['sensor'].shape[0] == 1:
            pass
        else:
            with torch.no_grad():
                pred = model(batch, device)
            truth = batch.y_dict['sensor'].view(pred.shape)
            if i == 0:
                y_pred = torch.zeros(len(dataloader), pred.shape[0], pred.shape[1])
                y_truth = torch.zeros(len(dataloader), pred.shape[0], pred.shape[1])
            truth = un_z_score(truth, dataloader.dataset.mean, dataloader.dataset.std_dev)
            pred = un_z_score(pred, dataloader.dataset.mean, dataloader.dataset.std_dev)
            y_pred[i, :pred.shape[0], :] = pred
            y_truth[i, :pred.shape[0], :] = truth
            rmse += RMSE(truth, pred)
            mae += MAE(truth, pred)
            mape += MAPE(truth, pred)
            n += 1
    rmse, mae, mape = rmse / n, mae / n, mape / n

    print(f'{type}, RMSE: {rmse}, MAE: {mae}, MAPE: {mape}')

    #get the average score for each metric in each batch
    return rmse, mae, mape, y_pred, y_truth

def train(model, device, dataloader, optimizer, loss_fn, epoch):
    model.train()
    for _, batch in enumerate(tqdm(dataloader, desc=f"Epoch {epoch}")):
        batch = batch.to(device)
        optimizer.zero_grad()
        y_pred = torch.squeeze(model(batch, device))
        loss = loss_fn()(y_pred.float(), torch.squeeze(batch.y_dict['sensor']).float())
        writer.add_scalar("Loss/train", loss, epoch)
        loss.backward()
        optimizer.step()

    return loss

# Make a tensorboard writer
writer = SummaryWriter()

def model_train(train_dataloader, val_dataloader, config, device):
    model = ST_GAT_EdgeType(in_channels=config['N_HIST'], out_channels=config['N_PRED'], n_nodes=config['N_NODE'], dropout=config['DROPOUT'])
    optimizer = optim.Adam(model.parameters(), lr=config['INITIAL_LR'], weight_decay=config['WEIGHT_DECAY'])
    loss_fn = torch.nn.MSELoss

    model.to(device)

    # For every epoch, train the model on training dataset. Evaluate model on validation dataset
    for epoch in range(config['EPOCHS']):
        loss = train(model, device, train_dataloader, optimizer, loss_fn, epoch)
        print(f"Loss: {loss:.3f}")
        if epoch % 5 == 0:
            train_mae, train_rmse, train_mape, _, _ = eval(model, device, train_dataloader, 'Train')
            val_mae, val_rmse, val_mape, _, _ = eval(model, device, val_dataloader, 'Valid')
            writer.add_scalar(f"MAE/train", train_mae, epoch)
            writer.add_scalar(f"RMSE/train", train_rmse, epoch)
            writer.add_scalar(f"MAPE/train", train_mape, epoch)
            writer.add_scalar(f"MAE/val", val_mae, epoch)
            writer.add_scalar(f"RMSE/val", val_rmse, epoch)
            writer.add_scalar(f"MAPE/val", val_mape, epoch)

    writer.flush()
    # Save the model
    timestr = time.strftime("%m-%d-%H%M%S")
    torch.save({
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "loss": loss,
            }, os.path.join(config["CHECKPOINT_DIR"], f"model_{timestr}.pt"))

    return model

def model_test(model, test_dataloader, device, config):
    rmse, mae, mape, y_pred, y_truth = eval(model, device, test_dataloader, 'Test')

###### Construct the Graph ######
    
def distance_to_W1(dist_df, conn_df):
    # Inverse transform distances
    dist_array = dist_df.values
    dist_array = np.where(dist_array == 0, np.nan, dist_array)
    dist_array_inv = 1 / dist_array
    dist_array_inv = pd.DataFrame(dist_array_inv).fillna(0).values
    
    # Mask with directional connectivity
    conn_array = conn_df.values
    W1 = dist_array_inv * conn_array
    
    # Mask with nearest sensor connectivity
    near_sen = np.zeros((W1.shape[0], W1.shape[0]))
    for sen in range(W1.shape[0]-1):
        no_neigh = False
        count = 1
        while W1[sen][sen+count] == 0:
            if count == (W1.shape[0]-sen-1):
                no_neigh = True
                break            
            count+=1

        if no_neigh:
            near_sen[sen][sen+count] = 0

        else:
            near_sen[sen][sen+count] = 1
    
    near_sen_sym = np.triu(near_sen) + np.triu(near_sen, 1).T # Make symmetric  
    W1 = W1 * near_sen_sym
    
    return W1

def distance_to_W2(dist_df, non_conn_df, dist_thresh, edge_num_thresh):
    # Inverse transform distances
    dist_array = dist_df.values
    dist_array = np.where(dist_array == 0, np.nan, dist_array)
    dist_array_inv = 1 / dist_array
    dist_array_inv = pd.DataFrame(dist_array_inv).fillna(0).values
    
    non_conn_array = non_conn_df.values
    W2 = dist_array_inv * non_conn_array
    
    dist_mask = W2 >= 1 / dist_thresh
    W2 = W2 * dist_mask

    edge_num_mask = []
    for row in W2:
        sorted_row = sorted(row)
        while sorted_row[-edge_num_thresh] == 0:
            edge_num_thresh -= 1
            if edge_num_thresh == 0:
                break

        thresh = sorted_row[-edge_num_thresh]
        edge_num_mask.append(row >= thresh)

    edge_num_mask = np.array(edge_num_mask)
    W2 = W2 * edge_num_mask
    
    W2_copy = W2.copy()
    for row_ind, row in enumerate(W2):
        for col_ind, val in enumerate(row):
            if val != 0:
                W2_copy[col_ind, row_ind] = val

    return W2_copy

def distance_to_W3(dist_df, conn_df, nth_jump, jump_dist_thresh, W1):
    dist_array = dist_df.values
    dist_array = np.where(dist_array == 0, np.nan, dist_array)
    dist_array_inv = 1 / dist_array
    dist_array_inv = pd.DataFrame(dist_array_inv).fillna(0).values

    # Mask with directional connectivity
    conn_array = conn_df.values
    W3 = dist_array_inv * conn_array
    W3 = W3 - W1

    nth_jump = 3
    jump_dist_thresh = 10
    for row_ind, row in enumerate(W3):
        row[:row_ind] = 0
        row_no_zero = row[row!=0]

        jump_weights = []
        for i in range(nth_jump-2,len(row_no_zero), nth_jump): # 1, 3
            jump_weights.append(row_no_zero[i])

        within_dist = np.array(jump_weights) > 1/jump_dist_thresh
        jump_weights = jump_weights * within_dist
        jump_weights = jump_weights[jump_weights!=0]

        for col_ind, val in enumerate(row):
            if val not in jump_weights:
                row[col_ind] = 0

    W3_copy = W3.copy()
    for row_ind, row in enumerate(W3):
        for col_ind, val in enumerate(row):
            if val != 0:
                W3_copy[col_ind, row_ind] = val
    
    return W3_copy

# Creating the graph
class Graph3(InMemoryDataset):
    def __init__(self, config, W1, W2, W3, root='', transform=None, pre_transform=None):
        self.config = config
        self.W1 = W1
        self.W2 = W2
        self.W3 = W3
        super().__init__(root, transform, pre_transform)
        self.process()
    
    def process(self):
        data = sensor_speed.T.values
        mean = np.mean(data)
        std_dev = np.std(data)
        data = z_score(data, mean, std_dev)
        
        n_node = data.shape[1]
        n_window = self.config['N_PRED'] + self.config['N_HIST']
        
        # W1
        W1_edge_index = torch.zeros((2, n_node**2), dtype=torch.long)
        W1_edge_attr = torch.zeros((n_node**2, 1))
        W1_num_edges = 0
        for i in range(n_node):
            for j in range(n_node):
                if self.W1[i, j] != 0:
                    W1_edge_index[0, W1_num_edges] = i
                    W1_edge_index[1, W1_num_edges] = j
                    W1_edge_attr[W1_num_edges] = self.W1[i, j]
                    W1_num_edges += 1
        
        # Resize to keep first num_edges entries
        W1_edge_ind_aslst = W1_edge_index.tolist()
        for i in range(len(W1_edge_ind_aslst[0])):
            if (W1_edge_ind_aslst[0][i] == 0) and (W1_edge_ind_aslst[1][i] == 0):
                first = W1_edge_ind_aslst[0][:i]
                second = W1_edge_ind_aslst[1][:i]
                W1_edge_index = torch.tensor([first, second], dtype=torch.long)
                break
        W1_edge_attr = W1_edge_attr.resize_(W1_num_edges, 1)
        
        # W2
        W2_edge_index = torch.zeros((2, n_node**2), dtype=torch.long)
        W2_edge_attr = torch.zeros((n_node**2, 1))
        W2_num_edges = 0
        for i in range(n_node):
            for j in range(n_node):
                if self.W2[i, j] != 0:
                    W2_edge_index[0, W2_num_edges] = i
                    W2_edge_index[1, W2_num_edges] = j
                    W2_edge_attr[W2_num_edges] = self.W2[i, j]
                    W2_num_edges += 1
        
        # Resize to keep first num_edges entries
        W2_edge_ind_aslst = W2_edge_index.tolist()
        for i in range(len(W2_edge_ind_aslst[0])):
            if (W2_edge_ind_aslst[0][i] == 0) and (W2_edge_ind_aslst[1][i] == 0):
                first = W2_edge_ind_aslst[0][:i]
                second = W2_edge_ind_aslst[1][:i]
                W2_edge_index = torch.tensor([first, second], dtype=torch.long)
                break
        W2_edge_attr = W2_edge_attr.resize_(W2_num_edges, 1)
        
        # W3
        W3_edge_index = torch.zeros((2, n_node**2), dtype=torch.long)
        W3_edge_attr = torch.zeros((n_node**2, 1))
        W3_num_edges = 0
        for i in range(n_node):
            for j in range(n_node):
                if self.W3[i, j] != 0:
                    W3_edge_index[0, W3_num_edges] = i
                    W3_edge_index[1, W3_num_edges] = j
                    W3_edge_attr[W3_num_edges] = self.W3[i, j]
                    W3_num_edges += 1
        
        # Resize to keep first num_edges entries
        W3_edge_ind_aslst = W3_edge_index.tolist()
        for i in range(len(W3_edge_ind_aslst[0])):
            if (W3_edge_ind_aslst[0][i] == 0) and (W3_edge_ind_aslst[1][i] == 0):
                first = W3_edge_ind_aslst[0][:i]
                second = W3_edge_ind_aslst[1][:i]
                W3_edge_index = torch.tensor([first, second], dtype=torch.long)
                break
        W3_edge_attr = W3_edge_attr.resize_(W3_num_edges, 1)
        
        sequences = []
        # T x F x N
        for i in range(self.config['N_DAYS']):
            for j in range(self.config['N_SLOT']):
                # for each time point construct a different graph with data object
                
                g = HeteroData()

                g['sensor', 'type1', 'sensor'].edge_index = W1_edge_index
                g['sensor', 'type2', 'sensor'].edge_index = W2_edge_index
                g['sensor', 'type3', 'sensor'].edge_index = W3_edge_index

                g['sensor', 'type1', 'sensor'].edge_attr = W1_edge_attr
                g['sensor', 'type2', 'sensor'].edge_attr = W2_edge_attr
                g['sensor', 'type3', 'sensor'].edge_attr = W3_edge_attr

                # (F,N) switched to (N,F)
                sta = i * self.config['N_DAY_SLOT'] + j
                end = sta + n_window
                
                # Find full window of speeds for each sensor
                full_window = np.swapaxes(data[sta:end, :], 0, 1)
                
                g['sensor'].x = torch.FloatTensor(full_window[:, 0:self.config['N_HIST']])
                g['sensor'].y = torch.FloatTensor(full_window[:, self.config['N_HIST']::])
                sequences.append(g)
        
        data, slices = self.collate(sequences)
        self.data, self.slices = data, slices
        self.n_node, self.mean, self.std_dev = n_node, mean, std_dev
        
    @property
    def processed_file_names(self):
        return []
    
# Creating the model
class ST_GAT_EdgeType(torch.nn.Module):
    def __init__(self, in_channels, out_channels, n_nodes, heads=8, dropout=0.0):
        super(ST_GAT_EdgeType, self).__init__()
        self.n_pred = out_channels
        self.heads = heads
        self.dropout = dropout
        self.n_nodes = n_nodes
        self.gat_in_dim = in_channels
        self.gat_out_dim = in_channels
        
        lstm1_hidden_size = 32
        lstm2_hidden_size = 128
        
        # single graph attentional layer with 8 attention heads
        self.gat = HeteroConv({
            ('sensor', 'type1', 'sensor'): GATv2Conv(in_channels=self.gat_in_dim, out_channels=self.gat_out_dim, heads=heads, dropout=0, concat=False),
            ('sensor', 'type2', 'sensor'): GATv2Conv(in_channels=self.gat_in_dim, out_channels=self.gat_out_dim, heads=heads, dropout=0, concat=False),
            ('sensor', 'type3', 'sensor'): GATv2Conv(in_channels=self.gat_in_dim, out_channels=self.gat_out_dim, heads=heads, dropout=0, concat=False)
        }, aggr='sum')

        # add two LSTM layers
        self.lstm1 = torch.nn.LSTM(input_size=self.n_nodes, hidden_size=lstm1_hidden_size, num_layers=1)
        for name, param in self.lstm1.named_parameters():
            if 'bias' in name:
                torch.nn.init.constant_(param, 0.0)
            elif 'weight' in name:
                torch.nn.init.xavier_uniform_(param)
        self.lstm2 = torch.nn.LSTM(input_size=lstm1_hidden_size, hidden_size=lstm2_hidden_size, num_layers=1)
        for name, param in self.lstm2.named_parameters():
            if 'bias' in name:
                torch.nn.init.constant_(param, 0.0)
            elif 'weight' in name:
                torch.nn.init.xavier_uniform_(param)

        # fully-connected neural network
        self.linear = torch.nn.Linear(lstm2_hidden_size, self.n_nodes*self.n_pred)
        torch.nn.init.xavier_uniform_(self.linear.weight)
        
    def forward(self, data, device):
        x_dict, edge_index_dict = data.x_dict, data.edge_index_dict
        if device == 'cpu':
            x_dict['sensor'] = torch.FloatTensor(x_dict['sensor'])
        else:
            x_dict['sensor'] = torch.cuda.FloatTensor(x_dict['sensor'])
            
        x_dict = self.gat(x_dict, edge_index_dict)
        x_dict = {key: x.relu() for key, x in x_dict.items()}
        x = F.dropout(x_dict['sensor'], self.dropout, training=self.training)

        batch_size = int(x.shape[0] / self.n_nodes)
        x = torch.reshape(x, (batch_size, self.n_nodes, x.shape[1]))
        x = torch.movedim(x, 2, 0)
        x, _ = self.lstm1(x)
        x, _ = self.lstm2(x)
        x = torch.squeeze(x[-1, :, :])
        x = self.linear(x)
        s = x.shape
        x = torch.reshape(x, (s[0], self.n_nodes, self.n_pred))
        x = torch.reshape(x, (s[0]*self.n_nodes, self.n_pred))

        return x
    
config = {
    'BATCH_SIZE': 50,
    'EPOCHS': 60,
    'WEIGHT_DECAY': 5e-5,
    'INITIAL_LR': 3e-4,
    'CHECKPOINT_DIR': './runs',
    'DROPOUT': 0.2,
    'N_HIST': 12,
    # number of possible 5 minute measurements per day
    'N_DAY_SLOT': 288,
    # number of days worth of data in the dataset
    'N_DAYS': 14,
    'N_NODE': 308,
    'W2_N_EDGE_THRESH': 3,
    'W2_DIST_THRESH': 2,
    'W3_NTH_JUMP': 3,
    'W3_JUMP_DIST_THRESH': 5
}

####### Predict the Next 15 Mins ######

config['N_PRED'] = 3

# Number of possible windows in a day
config['N_SLOT']= config['N_DAY_SLOT'] - (config['N_PRED']+config['N_HIST']) + 1

# Create Dataset
W1 = distance_to_W1(sensor_dist, sensor_conn)
W2 = distance_to_W2(sensor_dist, non_conn, config['W2_DIST_THRESH'], config['W2_N_EDGE_THRESH'])
W3 = distance_to_W3(sensor_dist, non_conn, config['W3_NTH_JUMP'], config['W3_JUMP_DIST_THRESH'], W1)
dataset = Graph3(config, W1, W2, W3)

# Create train, val, test splits
splits = (7, 3, 4) # 14 days in dataset -> train=7 val=3 test=4
d_train, d_val, d_test = get_splits(dataset, config['N_SLOT'], splits)

train_dataloader = DataLoader(d_train, batch_size=config['BATCH_SIZE'], shuffle=True)
val_dataloader = DataLoader(d_val, batch_size=config['BATCH_SIZE'], shuffle=True)
test_dataloader = DataLoader(d_test, batch_size=config['BATCH_SIZE'], shuffle=False)

# Get gpu if you can
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using {device}")

# Configure and train model
config['N_NODE'] = dataset.n_node
model = model_train(train_dataloader, val_dataloader, config, device)

def plot_prediction(test_dataloader, y_pred, y_truth, node, config):
    # Calculate the truth
    s = y_truth.shape
    y_truth = y_truth.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_truth = y_truth[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_truth = torch.flatten(y_truth)
    day1_truth = y_truth[config['N_SLOT']:2*config['N_SLOT']]


    # Calculate the predicted
    s = y_pred.shape
    y_pred = y_pred.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_pred = y_pred[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_pred = torch.flatten(y_pred)
    # Just grab the second day
    day1_pred = y_pred[config['N_SLOT']:2*config['N_SLOT']]
    t = [t for t in range(0, config['N_SLOT']*5, 5)]
    plt.plot(t, day1_pred, label='ST-GAT-EdgeType')
    plt.plot(t, day1_truth, label='truth')
    plt.xlabel('Time (minutes)')
    plt.ylabel('Speed prediction (mph)')
    plt.title('Predictions of traffic over one day at one sensor')
    plt.legend()
    plt.savefig(os.path.join(current_script_directory, '..','results', 'Graph3_EdgeType', 'Graph3_EdgeType_15.png'))
    plt.clf()

# Evaluate model on test set
rmse15, mae15, mape15, y_pred, y_truth = eval(model, device, test_dataloader, 'Test')
plot_prediction(test_dataloader, y_pred, y_truth, 0, config)

####### Predict the Next 30 Mins ######

config['N_PRED'] = 6

# Number of possible windows in a day
config['N_SLOT']= config['N_DAY_SLOT'] - (config['N_PRED']+config['N_HIST']) + 1

# Create Dataset
W1 = distance_to_W1(sensor_dist, sensor_conn)
W2 = distance_to_W2(sensor_dist, non_conn, config['W2_DIST_THRESH'], config['W2_N_EDGE_THRESH'])
W3 = distance_to_W3(sensor_dist, non_conn, config['W3_NTH_JUMP'], config['W3_JUMP_DIST_THRESH'], W1)
dataset = Graph3(config, W1, W2, W3)

# Create train, val, test splits
splits = (7, 3, 4) # 14 days in dataset -> train=7 val=3 test=4
d_train, d_val, d_test = get_splits(dataset, config['N_SLOT'], splits)

train_dataloader = DataLoader(d_train, batch_size=config['BATCH_SIZE'], shuffle=True)
val_dataloader = DataLoader(d_val, batch_size=config['BATCH_SIZE'], shuffle=True)
test_dataloader = DataLoader(d_test, batch_size=config['BATCH_SIZE'], shuffle=False)

# Get gpu if you can
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using {device}")

# Configure and train model
config['N_NODE'] = dataset.n_node
model = model_train(train_dataloader, val_dataloader, config, device)

def plot_prediction(test_dataloader, y_pred, y_truth, node, config):
    # Calculate the truth
    s = y_truth.shape
    y_truth = y_truth.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_truth = y_truth[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_truth = torch.flatten(y_truth)
    day1_truth = y_truth[config['N_SLOT']:2*config['N_SLOT']]


    # Calculate the predicted
    s = y_pred.shape
    y_pred = y_pred.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_pred = y_pred[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_pred = torch.flatten(y_pred)
    # Just grab the second day
    day1_pred = y_pred[config['N_SLOT']:2*config['N_SLOT']]
    t = [t for t in range(0, config['N_SLOT']*5, 5)]
    plt.plot(t, day1_pred, label='ST-GAT-EdgeType')
    plt.plot(t, day1_truth, label='truth')
    plt.xlabel('Time (minutes)')
    plt.ylabel('Speed prediction (mph)')
    plt.title('Predictions of traffic over one day at one sensor')
    plt.legend()
    plt.savefig(os.path.join(current_script_directory, '..','results', 'Graph3_EdgeType', 'Graph3_EdgeType_30.png'))
    plt.clf()

# Evaluate model on test set
rmse30, mae30, mape30, y_pred, y_truth = eval(model, device, test_dataloader, 'Test')
plot_prediction(test_dataloader, y_pred, y_truth, 0, config)

####### Predict the Next 45 Mins ######

config['N_PRED'] = 9

# Number of possible windows in a day
config['N_SLOT']= config['N_DAY_SLOT'] - (config['N_PRED']+config['N_HIST']) + 1

# Create Dataset
W1 = distance_to_W1(sensor_dist, sensor_conn)
W2 = distance_to_W2(sensor_dist, non_conn, config['W2_DIST_THRESH'], config['W2_N_EDGE_THRESH'])
W3 = distance_to_W3(sensor_dist, non_conn, config['W3_NTH_JUMP'], config['W3_JUMP_DIST_THRESH'], W1)
dataset = Graph3(config, W1, W2, W3)

# Create train, val, test splits
splits = (7, 3, 4) # 14 days in dataset -> train=7 val=3 test=4
d_train, d_val, d_test = get_splits(dataset, config['N_SLOT'], splits)

train_dataloader = DataLoader(d_train, batch_size=config['BATCH_SIZE'], shuffle=True)
val_dataloader = DataLoader(d_val, batch_size=config['BATCH_SIZE'], shuffle=True)
test_dataloader = DataLoader(d_test, batch_size=config['BATCH_SIZE'], shuffle=False)

# Get gpu if you can
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using {device}")

# Configure and train model
config['N_NODE'] = dataset.n_node
model = model_train(train_dataloader, val_dataloader, config, device)

def plot_prediction(test_dataloader, y_pred, y_truth, node, config):
    # Calculate the truth
    s = y_truth.shape
    y_truth = y_truth.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_truth = y_truth[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_truth = torch.flatten(y_truth)
    day1_truth = y_truth[config['N_SLOT']:2*config['N_SLOT']]


    # Calculate the predicted
    s = y_pred.shape
    y_pred = y_pred.reshape(s[0], config['BATCH_SIZE'], config['N_NODE'], s[-1])
    # just get the first prediction out for the nth node
    y_pred = y_pred[:, :, node, 0]
    # Flatten to get the predictions for entire test dataset
    y_pred = torch.flatten(y_pred)
    # Just grab the second day
    day1_pred = y_pred[config['N_SLOT']:2*config['N_SLOT']]
    t = [t for t in range(0, config['N_SLOT']*5, 5)]
    plt.plot(t, day1_pred, label='ST-GAT-EdgeType')
    plt.plot(t, day1_truth, label='truth')
    plt.xlabel('Time (minutes)')
    plt.ylabel('Speed prediction (mph)')
    plt.title('Predictions of traffic over one day at one sensor')
    plt.legend()
    plt.savefig(os.path.join(current_script_directory, '..','results', 'Graph3_EdgeType', 'Graph3_EdgeType_45.png'))
    plt.clf()

# Evaluate model on test set
rmse45, mae45, mape45, y_pred, y_truth = eval(model, device, test_dataloader, 'Test')
plot_prediction(test_dataloader, y_pred, y_truth, 0, config)

print('-------------------------------------------------------------------------------')
print('\nGraph 3 Edge Type')
print('-----------------')
print('Node Features: Speeds from Past Hour')
print('Edges Included: Type 1, Type 2, Type 3')
print('Edge Types: Learned\n')
print(f'Test Evals for 15 mins: RMSE: {rmse15}, MAE: {mae15}, MAPE: {mape15}')
print(f'Test Evals for 30 mins: RMSE: {rmse30}, MAE: {mae30}, MAPE: {mape30}')
print(f'Test Evals for 45 mins: RMSE: {rmse45}, MAE: {mae45}, MAPE: {mape45}')