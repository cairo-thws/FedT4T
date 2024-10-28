import torch
from torch.utils.data import DataLoader
from torchvision.transforms import ToTensor, Normalize, Compose
from datasets import load_dataset
import matplotlib.pyplot as plt
from collections import Counter
import random
import numpy as np
from PIL import Image
import io
from model import Net
from client import FlowerClient
from flwr_datasets import FederatedDataset
from flwr_datasets.partitioner import IidPartitioner, DirichletPartitioner
from flwr_datasets.visualization import plot_label_distributions
from flwr.common import Context
from flwr.client import ClientApp
from flwr.common import ndarrays_to_parameters
from flwr.server import ServerApp, ServerConfig, ServerAppComponents
from flwr.server.strategy import FedAvg
from typing import List, Tuple
from flwr.common import Metrics
from flwr.simulation import run_simulation
import axelrod as axl
from ipd_tournament_server import Ipd_TournamentServer
from flwr.server.client_manager import SimpleClientManager

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

# NUM_PARTITIONS = 20 # determined by number of strategies
num_rounds = 20
SEED = 42
plot_label_distribution_over_clients = False
strategy_mem_depth = 1

def sow_seed(seed):
    torch.manual_seed(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.use_deterministic_algorithms(True)
    

def visualise_n_random_examples(trainset_, n: int, verbose: bool = True):
    trainset_data = [
        Image.open(io.BytesIO(entry[0].as_py())) for entry in trainset_.data[0]
    ]
    idx = list(range(len(trainset_data)))
    random.shuffle(idx)
    idx = idx[:n]
    if verbose:
        print(f"will display images with idx: {idx}")

    # construct canvas
    num_cols = 8
    num_rows = int(np.ceil(len(idx) / num_cols))
    fig, axs = plt.subplots(figsize=(16, num_rows * 2), nrows=num_rows, ncols=num_cols)

    # display images on canvas
    for c_i, i in enumerate(idx):
        axs.flat[c_i].imshow(trainset_data[i], cmap="gray")

def get_mnist_dataloaders(mnist_dataset, batch_size: int):
    pytorch_transforms = Compose([ToTensor(), Normalize((0.1307,), (0.3081,))])

    # Prepare transformation functions
    def apply_transforms(batch):
        batch["image"] = [pytorch_transforms(img) for img in batch["image"]]
        return batch

    mnist_train = mnist_dataset["train"].with_transform(apply_transforms)
    mnist_test = mnist_dataset["test"].with_transform(apply_transforms)

    # Construct PyTorch dataloaders
    trainloader = DataLoader(mnist_train, batch_size=batch_size, shuffle=True)
    testloader = DataLoader(mnist_test, batch_size=batch_size)
    return trainloader, testloader

def train(net, trainloader, optimizer, epochs):
    """Train the network on the training set."""
    criterion = torch.nn.CrossEntropyLoss()
    net.train()
    for batch in trainloader:
        images, labels = batch["image"], batch["label"]
        optimizer.zero_grad()
        loss = criterion(net(images), labels)
        loss.backward()
        optimizer.step()

def test(net, testloader):
    """Validate the network on the entire test set."""
    criterion = torch.nn.CrossEntropyLoss()
    correct, loss = 0, 0.0
    net.eval()
    with torch.no_grad():
        for batch in testloader:
            images, labels = batch["image"], batch["label"]
            outputs = net(images)
            loss += criterion(outputs, labels).item()
            _, predicted = torch.max(outputs.data, 1)
            correct += (predicted == labels).sum().item()
    accuracy = correct / len(testloader.dataset)
    return loss, accuracy

def get_params(model):
    """Extract model parameters as a list of NumPy arrays."""
    return [val.cpu().numpy() for _, val in model.state_dict().items()]

def run_centralised(
    trainloader, testloader, epochs: int, lr: float, momentum: float = 0.9
):
    """A minimal (but complete) training loop"""

    # instantiate the model
    model = Net(num_classes=10)

    # define optimiser with hyperparameters supplied
    optim = torch.optim.SGD(model.parameters(), lr=lr, momentum=momentum)

    # train for the specified number of epochs
    for e in range(epochs):
        print(f"Training epoch {e} ...")
        train(model, trainloader, optim, epochs)

    # training is completed, then evaluate model on the test set
    loss, accuracy = test(model, testloader)
    print(f"{loss = }")
    print(f"{accuracy = }")
    
def client_fn(context: Context):
    """Returns a FlowerClient containing its data partition."""
   
    partition_id = int(context.node_config["partition-id"])
    partition = fds.load_partition(partition_id, "train")
    # partition into train/validation
    partition_train_val = partition.train_test_split(test_size=0.1, seed=SEED)

    # Let's use the function defined earlier to construct the dataloaders
    # and apply the dataset transformations
    trainloader, testloader = get_mnist_dataloaders(partition_train_val, batch_size=32)
    
    # Pop last element from list and set seed
    client_ipd_strat = client_strategies[partition_id]
    client_ipd_strat.set_seed(SEED)

    print("Init client(" + str(partition_id) + ") with strategy " + client_ipd_strat.name)

    return FlowerClient(trainloader=trainloader,
                        valloader=testloader,
                        ipd_strategy=client_ipd_strat,
                        client_id=partition_id).to_client()

def server_fn(context: Context):

    # instantiate the model
    model = Net(num_classes=10)
    ndarrays = get_params(model)
    # Convert model parameters to flwr.common.Parameters
    global_model_init = ndarrays_to_parameters(ndarrays)

    # Define the strategy
    strategy = FedAvg(
        fraction_fit=0.5,  # 50% clients sampled each round to do fit()
        fraction_evaluate=0.1,  # 10% clients sample each round to do evaluate()
        #min_fit_clients= 16,
        evaluate_metrics_aggregation_fn=weighted_average,  # callback defined earlier
        initial_parameters=global_model_init,  # initialised global model
    )
    
    # Iterated Prisoners Dilemma Tournament Server
    ipd_tournament_server= Ipd_TournamentServer(client_manager=SimpleClientManager(), strategy=strategy)

    # Construct ServerConfig
    config = ServerConfig(num_rounds=num_rounds)

    # Wrap everything into a `ServerAppComponents` object
    return ServerAppComponents(server=ipd_tournament_server, config=config)

# Define metric aggregation function
def weighted_average(metrics: List[Tuple[int, Metrics]]) -> Metrics:
    # Multiply accuracy of each client by number of examples used
    accuracies = [num_examples * m["accuracy"] for num_examples, m in metrics]
    examples = [num_examples for num_examples, _ in metrics]

    # Aggregate and return custom metric (weighted average)
    return {"accuracy": sum(accuracies) / sum(examples)}

###################### MAIN TRACK ######################

# initialize strategies with memory_depth eq. 1
client_strategies = [s() for s  in axl.filtered_strategies(filterset={'memory_depth': strategy_mem_depth}, strategies=axl.ordinary_strategies)]
# extend to random
client_strategies.append(axl.Random())
# mix list
random.shuffle(client_strategies)
# create partitions for each FL client
NUM_PARTITIONS = len(client_strategies)
#print(*client_strategies, "\\n")
print("Strategies initialized: " + str(NUM_PARTITIONS))

# initialize data partitions
iid_partitioner = IidPartitioner(num_partitions=NUM_PARTITIONS)
dirichlet_partitioner = DirichletPartitioner(num_partitions=NUM_PARTITIONS, alpha=0.1, partition_by="label", seed=SEED, min_partition_size=0)
# Let's partition the "train" split of the MNIST dataset
# The MNIST dataset will be downloaded if it hasn't been already
fds = FederatedDataset(dataset="ylecun/mnist", partitioners={"train": dirichlet_partitioner})


def main():
    if plot_label_distribution_over_clients:
        # pre-load partitions
        partitioner = fds.partitioners["train"]

        fig, ax, df = plot_label_distributions(
            partitioner,
            label_name="label",
            plot_type="bar",
            size_unit="absolute",
            partition_id_axis="x",
            legend=True,
            verbose_labels=True,
            max_num_partitions=30,  # Note we are only showing the first 30 so the plot remains readable
            title="Per Partition Labels Distribution",
        )

        plt.show()


    # Concstruct the ClientApp passing the client generation function
    client_app = ClientApp(client_fn=client_fn)

    # Create your ServerApp
    server_app = ServerApp(server_fn=server_fn)

    run_simulation(
        server_app=server_app, client_app=client_app, num_supernodes=NUM_PARTITIONS
    )


if __name__ == '__main__':
    sow_seed(SEED)
    main()