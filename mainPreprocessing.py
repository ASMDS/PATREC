import os

from utils.DatasetOptions import DatasetOptions
from preprocessing.Preprocessor import Preprocessor


dirProject = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/';
dirData = dirProject + 'data/';

dict_dataset_options = {
    'dir_data':                 dirData,
    'data_prefix':              'patrec',
    'dataset':                  '20122015',
    'encoding':                 'categorical',
    'featureset':               'newfeatures'
}

options = DatasetOptions(dict_dataset_options);
preproc = Preprocessor(options);
# preproc.splitColumns();
# preproc.clean()
preproc.group()
preproc.createFeatureSet()
preproc.encodeFeatures();
preproc.fuse();
