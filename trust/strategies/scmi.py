from .strategy import Strategy
import numpy as np

import torch
from torch import nn
from scipy import stats
import submodlib

class SCMI(Strategy):
    
    """
    This strategy implements the Submodular Conditional Mutual Information (SCMI) selection paradigm discuss in the paper 
    SIMILAR: Submodular Information Measures Based Active Learning In Realistic Scenarios :footcite:`kothawade2021similar`. In this selection 
    paradigm, points from the unlabeled dataset are chosen in such a way that the submodular conditional mutual information 
    between this set of points and a provided query set is maximized, conditioned on a private dataset. 
    Doing so allows a practitioner to select points from an unlabeled set that are SIMILAR to points that they have 
    provided in the query set while being dissimilar to points provided in the private set.
    
    These submodular conditional mutual information functions rely on formulating embeddings for the points in the query set, 
    the unlabeled set, and the private set. Once these embeddings are formed, similarity kernels are formed from these 
    embeddings based on a similarity metric. Once these similarity kernels are formed, they are used in computing the value 
    of each submodular conditional mutual information function. Hence, common techniques for submodular maximization 
    subject to a cardinality constraint can be used, such as the naive greedy algorithm, the lazy greedy algorithm, and so forth.
    
    In this framework, we set the cardinality constraint to be the active learning selection budget; hence, a list of 
    indices with a total length less than or equal to this cardinality constraint will be returned. Depending on the 
    maximization configuration, one can ensure that the length of this list will be equal to the cardinality constraint.
    
    Currently, two submodular conditional mutual information functions are implemented: 'flcmi' and 'logdetcmi'. Each
    function is obtained by applying the definition of a submodular conditional mutual information function using common 
    submodular functions. For more information-theoretic discussion, consider referring to the paper Submodular Combinatorial 
    Information Measures with Applications in Machine Learning :footcite:`iyer2021submodular`.
    
    Parameters
    ----------
    labeled_dataset: torch.utils.data.Dataset
        The labeled dataset to be used in this strategy. For the purposes of selection, the labeled dataset is not used, 
        but it is provided to fit the common framework of the Strategy superclass.
    unlabeled_dataset: torch.utils.data.Dataset
        The unlabeled dataset to be used in this strategy. It is used in the selection process as described above.
        Importantly, the unlabeled dataset must return only a data Tensor; if indexing the unlabeled dataset returns a tuple of 
        more than one component, unexpected behavior will most likely occur.
    query_dataset: torch.utils.data.Dataset
        The query dataset to be used in this strategy. It is used in the selection process as described above. Notably, 
        the query dataset should be labeled; hence, indexing the query dataset should return a data/label pair. This is 
        done in this fashion to allow for gradient embeddings.
    private_dataset: torch.utils.data.Dataset
        The private dataset to be used in this strategy. It is used in the selection process as described above. Notably, 
        the private dataset should be labeled; hence, indexing the query dataset should return a data/label pair. This is 
        done in this fashion to allow for gradient embeddings.
    net: torch.nn.Module
        The neural network model to use for embeddings and predictions. Notably, all embeddings typically come from extracted 
        features from this network or from gradient embeddings based on the loss, which can be based on hypothesized gradients 
        or on true gradients (depending on the availability of the label).
    nclasses: int
        The number of classes being predicted by the neural network.
    args: dict
        A dictionary containing many configurable settings for this strategy. Each key-value pair is described below:
            'batch_size': int
                The batch size used internally for torch.utils.data.DataLoader objects. Default: 1
            'device': string
                The device to be used for computation. PyTorch constructs are transferred to this device. Usually is one 
                of 'cuda' or 'cpu'. Default: 'cuda' if a CUDA-enabled device is available; otherwise, 'cpu'
            'loss': function
                The loss function to be used in computations. Default: torch.nn.functional.cross_entropy
            'scmi_function': string
                The submodular mutual information function to use in optimization. Must be one of 'flcmi' or 'logdetcmi'. 
                REQUIRED
            'optimizer': string
                The optimizer to use for submodular maximization. Can be one of 'NaiveGreedy', 'StochasticGreedy', 
                'LazyGreedy' and 'LazierThanLazyGreedy'. Default: 'NaiveGreedy'
            'metric': string
                The similarity metric to use for similarity kernel computation. This can be either 'cosine' or 'euclidean'. 
                Default: 'cosine'
            'eta': float
                A magnification constant that is used in all but gcmi. It is used as a value of query-relevance vs diversity 
                trade-off. Increasing eta tends to increase query-relevance while reducing query-coverage and diversity. 
                Default: 1
            'nu': float
                A parameter that governs the hardness of the privacy constraint. Default: 1.
            'embedding_type': string
                The type of embedding to compute for similarity kernel computation. This can be either 'gradients' or 
                'features'. Default: 'gradients'
            'gradType': string
                When 'embedding_type' is 'gradients', this defines the type of gradient to use. 'bias' creates gradients from 
                the loss function with respect to the biases outputted by the model. 'linear' creates gradients from the 
                loss function with respect to the last linear layer features. 'bias_linear' creates gradients from the 
                loss function using both. Default: 'bias_linear'
            'layer_name': string
                When 'embedding_type' is 'features', this defines the layer within the neural network that is used to extract 
                feature embeddings. Namely, this argument must be the name of a module used in the forward() computation of 
                the model. Default: 'avgpool'
            'stopIfZeroGain': bool
                Controls if the optimizer should cease maximization if there is zero gain in the submodular objective.
                Default: False
            'stopIfNegativeGain': bool
                Controls if the optimizer should cease maximization if there is negative gain in the submodular objective.
                Default: False
            'verbose': bool
                Gives a more verbose output when calling select() when True. Default: False
    """
    
    def __init__(self, labeled_dataset, unlabeled_dataset, query_dataset, private_dataset, net, nclasses, args={}): #
        
        super(SCMI, self).__init__(labeled_dataset, unlabeled_dataset, net, nclasses, args)        
        self.query_dataset = query_dataset
        self.private_dataset = private_dataset

    def select(self, budget):
        """
        Selects a set of points from the unlabeled dataset to label based on this strategy's methodology.
        
        Parameters
        ----------
        budget: int
            Number of points to choose from the unlabeled dataset
        
        Returns
        ----------
        chosen: list
            List of selected data point indices with respect to the unlabeled dataset
        """ 

        #Get hyperparameters from args dict
        optimizer = self.args['optimizer'] if 'optimizer' in self.args else 'NaiveGreedy'
        metric = self.args['metric'] if 'metric' in self.args else 'cosine'
        eta = self.args['eta'] if 'eta' in self.args else 1
        nu = self.args['nu'] if 'nu' in self.args else 1
        gradType = self.args['gradType'] if 'gradType' in self.args else "bias_linear"
        stopIfZeroGain = self.args['stopIfZeroGain'] if 'stopIfZeroGain' in self.args else False
        stopIfNegativeGain = self.args['stopIfNegativeGain'] if 'stopIfNegativeGain' in self.args else False
        verbose = self.args['verbose'] if 'verbose' in self.args else False
        embedding_type = self.args['embedding_type'] if 'embedding_type' in self.args else "gradients"
        if(embedding_type=="features"):
            layer_name = self.args['layer_name'] if 'layer_name' in self.args else "avgpool"
        keep_embedding = self.args['keep_embedding'] if 'keep_embedding' in self.args else False

        #Compute Embeddings
        if(embedding_type == "gradients"):
            unlabeled_data_embedding = self.get_grad_embedding(self.unlabeled_dataset, True, gradType)
            query_embedding = self.get_grad_embedding(self.query_dataset, False, gradType)
            private_embedding = self.get_grad_embedding(self.private_dataset, False, gradType)
        elif(embedding_type == "features"):
            unlabeled_data_embedding = self.get_feature_embedding(self.unlabeled_dataset, True, layer_name)
            query_embedding = self.get_feature_embedding(self.query_dataset, False, layer_name)
            private_embedding = self.get_feature_embedding(self.private_dataset, False, layer_name)
        else:
            raise ValueError("Provided representation must be one of gradients or features")

        if(keep_embedding):
            self.unlabeled_data_embedding = unlabeled_data_embedding
            self.private_embedding = private_embedding
            self.query_embedding = query_embedding

        #Compute image-image kernel
        data_sijs = submodlib.helper.create_kernel(X=unlabeled_data_embedding.cpu().numpy(), metric=metric, method="sklearn")
        #Compute query-query kernel
        if(self.args['scmi_function']=='logdetmi'):
            query_query_sijs = submodlib.helper.create_kernel(X=query_embedding.cpu().numpy(), metric=metric, method="sklearn")
            private_private_sijs = submodlib.helper.create_kernel(X=private_embedding.cpu().numpy(), metric=metric, method="sklearn")
            query_private_sijs = submodlib.helper.create_kernel(X=private_embedding.cpu().numpy(), X_rep=query_embedding.cpu().numpy(), metric=metric, method="sklearn")
        #Compute image-query kernel
        query_sijs = submodlib.helper.create_kernel(X=query_embedding.cpu().numpy(), X_rep=unlabeled_data_embedding.cpu().numpy(), metric=metric, method="sklearn")
        private_sijs = submodlib.helper.create_kernel(X=private_embedding.cpu().numpy(), X_rep=unlabeled_data_embedding.cpu().numpy(), metric=metric, method="sklearn")
        
        if(self.args['scmi_function']=='flcmi'):
            obj = submodlib.FacilityLocationConditionalMutualInformationFunction(n=unlabeled_data_embedding.shape[0],
                                                                      num_queries=query_embedding.shape[0],
                                                                      num_privates=private_embedding.shape[0], 
                                                                      data_sijs=data_sijs, 
                                                                      query_sijs=query_sijs, 
                                                                      private_sijs=private_sijs, 
                                                                      magnificationEta=eta,
                                                                      privacyHardness=nu)
    
        if(self.args['scmi_function']=='logdetcmi'):
            lambdaVal = self.args['lambdaVal'] if 'lambdaVal' in self.args else 1
            obj = submodlib.LogDeterminantConditionalMutualInformationFunction(n=unlabeled_data_embedding.shape[0],
                                                                      num_queries=query_embedding.shape[0],
                                                                      num_privates=private_embedding.shape[0], 
                                                                      data_sijs=data_sijs, 
                                                                      query_sijs=query_sijs, 
                                                                      private_sijs=private_sijs,
                                                                      query_query_sijs=query_query_sijs,
                                                                      private_private_sijs=private_private_sijs,
                                                                      query_private_sijs=query_private_sijs, 
                                                                      magnificationEta=eta,
                                                                      privacyHardness=nu,
                                                                      lambdaVal=lambdaVal)

        greedyList = obj.maximize(budget=budget,optimizer=optimizer, stopIfZeroGain=stopIfZeroGain, 
                              stopIfNegativeGain=stopIfNegativeGain, verbose=verbose)
        greedyIndices = [x[0] for x in greedyList]
        return greedyIndices