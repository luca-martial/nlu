import logging

from nlu.extractors.extraction_resolver_OS import OS_anno2config
from nlu.extractors.extractor_methods.base_extractor_methods import *

logger = logging.getLogger('nlu')
import nlu
import nlu.pipe.pipe_components
import sparknlp
from  typing import List, Dict

from sparknlp.base import *
from sparknlp.base import LightPipeline
from sparknlp.annotator import *
import pyspark
from pyspark.sql.types import ArrayType, FloatType, DoubleType
from pyspark.sql.functions import col as pyspark_col
from pyspark.sql.functions import monotonically_increasing_id, greatest, expr,udf
import pandas as pd
import numpy as np
from pyspark.sql.types import StructType,StructField, StringType, IntegerType
from nlu.pipe.component_resolution import extract_classifier_metadata_from_nlu_ref
from nlu.pipe.storage_ref_utils import StorageRefUtils
from nlu.pipe.component_utils import ComponentUtils
from nlu.pipe.output_level_resolution_utils import OutputLevelUtils


class BasePipe(dict):
    # we inherhit from dict so the pipe is indexable and we have a nice shortcut for accessing the spark nlp model
    def __init__(self):
        self.nlu_ref=''
        self.raw_text_column = 'text'
        self.raw_text_matrix_slice = 1  # place holder for getting text from matrix
        self.spark_nlp_pipe = None
        self.has_trainable_components = False
        self.needs_fitting = True
        self.is_fitted = False
        self.output_positions = False  # Wether to putput positions of Features in the final output. E.x. positions of tokens, entities, dependencies etc.. inside of the input document.
        self.output_level = ''  # either document, chunk, sentence, token
        self.output_different_levels = True
        self.light_pipe_configured = False
        self.spark_non_light_transformer_pipe = None
        self.components = []  # orderd list of nlu_component objects
        self.output_datatype = 'pandas'  # What data type should be returned after predict either spark, pandas, modin, numpy, string or array
        self.lang = 'en'
    def isInstanceOfNlpClassifer(self, model):
        '''
        Check for a given Spark NLP model if it is an instance of a classifier , either approach or already fitted transformer will return true
        This is used to configured the input/output columns based on the inputs
        :param model: the model to check
        :return: True if it is one of the following classes : (ClassifierDLModel,ClassifierDLModel,MultiClassifierDLModel,MultiClassifierDLApproach,SentimentDLModel,SentimentDLApproach) )
        '''
        return isinstance(model, (
            ClassifierDLModel, ClassifierDLModel, MultiClassifierDLModel, MultiClassifierDLApproach, SentimentDLModel,
            SentimentDLApproach))

    def configure_outputs(self, component, nlu_reference, name_to_add=''):
        '''
        Configure output column names of classifiers from category to something more meaningful
        Name should be Name of classifier, based on NLU reference.
        Duplicate names will be resolved by appending suffix "_i" to column name, based on how often we encounterd duplicate errors
        This updates component infos accordingly
        :param component: classifier component for which the output columns to  configured
        :param nlu_reference: nlu reference from which is component stemmed
        :return: None
        '''
        if nlu_reference == 'default_name' : return
        nlu_reference = nlu_reference.replace('train.', '')
        model_meta = extract_classifier_metadata_from_nlu_ref(nlu_reference)
        can_use_name = False
        new_output_name = model_meta[0]
        i = 0
        while can_use_name == False:
            can_use_name = True
            for c in self.components:
                if new_output_name in c.info.spark_input_column_names + c.info.spark_output_column_names and c.info.name != component.info.name:
                    can_use_name = False
        if can_use_name == False:
            new_output_name = new_output_name + '_' + str(i)
            i += 1
        # classifiers always have just 1 output col
        logger.info(f"Configured output columns name to {new_output_name} for classifier in {nlu_reference}")
        component.model.setOutputCol(new_output_name)
        component.info.spark_output_column_names = [new_output_name]

    def add(self, component, nlu_reference="default_name", pretrained_pipe_component=False, name_to_add=''):
        '''

        :param component:
        :param nlu_reference: NLU references, passed for components that are used specified and not automatically generate by NLU
        :return:
        '''
        if hasattr(component.info,'nlu_ref'): nlu_reference = component.info.nlu_ref

        # self.nlu_reference = component.info.nlu_ref if component.info

        self.components.append(component)
        # ensure that input/output cols are properly set
        # Spark NLP model reference shortcut

        name = component.info.name.replace(' ', '').replace('train.', '')

        if StorageRefUtils.has_storage_ref(component):
            name = name +'@' + StorageRefUtils.extract_storage_ref(component)
        logger.info(f"Adding {name} to internal pipe")



        # Configure output column names of classifiers from category to something more meaningful
        if self.isInstanceOfNlpClassifer(component.model): self.configure_outputs(component, nlu_reference)

        if name_to_add == '':
            # Add Component as self.index and in attributes
            if 'embed' in component.info.type and nlu_reference not in self.keys() and not pretrained_pipe_component:
                self[name] = component.model
            elif name not in self.keys():
                if not hasattr(component.info,'nlu_ref'):  component.info.nlu_ref  = nlu_reference
                self[name] = component.model
            else:
                nlu_identifier = ComponentUtils.get_nlu_ref_identifier(component)
                component.info.nlu_ref = nlu_reference
                self[component.info.name +"@"+ nlu_identifier] = component.model
        else :self[name_to_add] = component.model

class NLUPipeline(BasePipe):
    def __init__(self):
        super().__init__()
        """ Initializes a pretrained pipeline         """
        self.spark = sparknlp.start()
        self.provider = 'sparknlp'
        self.pipe_ready = False  # ready when we have created a spark df
        # The NLU pipeline uses  types of Spark NLP annotators to identify how to handle different columns
        self.levels = {
            'token': ['token', 'pos', 'ner', 'lemma', 'lem', 'stem', 'stemm', 'word_embeddings', 'named_entity',
                      'entity', 'dependency',
                      'labeled_dependency', 'dep', 'dep.untyped', 'dep.typed'],
            'sentence': ['sentence', 'sentence_embeddings', ] + ['sentiment', 'classifer', 'category'],
            'chunk': ['chunk', 'embeddings_chunk', 'chunk_embeddings'],
            'document': ['document', 'language'],
            'embedding_level': []
            # ['sentiment', 'classifer'] # WIP, wait for Spark NLP Getter/Setter fixes to implement this properly
            # embedding level  annotators output levels depend on the level of the embeddings they are fed. If we have Doc/Chunk/Word/Sentence embeddings, those annotators output at the same level.

        }


        self.annotator_levels_approach_based = {
            'document': [DocumentAssembler, Chunk2Doc,
                         YakeModel,
                         ],
            'sentence': [SentenceDetector, SentenceDetectorDLApproach, ],
            'chunk': [Chunker, ChunkEmbeddings,  ChunkTokenizer, Token2Chunk, TokenAssembler,
                      NerConverter, Doc2Chunk,NGramGenerator],
            'token': [ NerCrfApproach, NerDLApproach,
                       PerceptronApproach,
                       Stemmer,
                       ContextSpellCheckerApproach,
                       nlu.WordSegmenter,
                       Lemmatizer,LemmatizerModel, TypedDependencyParserApproach, DependencyParserApproach,
                       Tokenizer, RegexTokenizer, RecursiveTokenizer
                , DateMatcher, TextMatcher, BigTextMatcher, MultiDateMatcher,
                       WordSegmenterApproach
                       ],
            # 'sub_token': [StopWordsCleaner, DateMatcher, TextMatcher, BigTextMatcher, MultiDateMatcher],
            # these can be document or sentence
            'input_dependent': [ViveknSentimentApproach, SentimentDLApproach, ClassifierDLApproach,
                                LanguageDetectorDL,
                                MultiClassifierDLApproach,  SentenceEmbeddings, NorvigSweetingApproach,
                                ],

            # 'unclassified': [Yake, Ngram]
        }


        self.annotator_levels_model_based = {
            'document': [],
            'sentence': [SentenceDetectorDLModel, ],
            'chunk': [ChunkTokenizerModel, ChunkTokenizerModel,  ],
            'token': [ContextSpellCheckerModel, AlbertEmbeddings, BertEmbeddings, ElmoEmbeddings, WordEmbeddings,
                      XlnetEmbeddings, WordEmbeddingsModel,
                      # NER models are token level, they give IOB predictions and cofidences for EVERY token!
                      NerDLModel, NerCrfModel, PerceptronModel, SymmetricDeleteModel, NorvigSweetingModel,
                      ContextSpellCheckerModel,
                      TypedDependencyParserModel, DependencyParserModel,
                      RecursiveTokenizerModel,
                      TextMatcherModel, BigTextMatcherModel, RegexMatcherModel,
                      WordSegmenterModel, TokenizerModel
                      ],
            # 'sub_token': [TextMatcherModel, BigTextMatcherModel, RegexMatcherModel, ],
            # sub token is when annotator is token based but some tokens may be missing since dropped/cleanes

            'sub_token' : [
                StopWordsCleaner

            ] ,
            'input_dependent': [BertSentenceEmbeddings, UniversalSentenceEncoder, ViveknSentimentModel,
                                SentimentDLModel, MultiClassifierDLModel, MultiClassifierDLModel, ClassifierDLModel,
                                MarianTransformer,T5Transformer

                                ],
        }

        self.all_embeddings = {
            'token' : [AlbertEmbeddings, BertEmbeddings, ElmoEmbeddings, WordEmbeddings,
                       XlnetEmbeddings,WordEmbeddingsModel],
            'input_dependent' : [SentenceEmbeddings, UniversalSentenceEncoder,BertSentenceEmbeddings]

        }
    def get_sample_spark_dataframe(self):
        data = {"text": ['This day sucks', 'I love this day', 'I dont like Sami']}
        text_df = pd.DataFrame(data)
        return sparknlp.start().createDataFrame(data=text_df)
    def verify_all_labels_exist(self,dataset):
        #todo
        return True
        # pass
    def fit(self, dataset=None, dataset_path=None, label_seperator=','):
        # if dataset is  string with '/' in it, its dataset path!
        '''
        Converts the input Pandas Dataframe into a Spark Dataframe and trains a model on it.
        :param dataset: The pandas dataset to train on, should have a y column for label and 'text' column for text features
        :param dataset_path: Path to a CONLL2013 format dataset. It will be read for NER and POS training.
        :param label_seperator: If multi_classifier is trained, this seperator is used to split the elements into an Array column for Pyspark
        :return: A nlu pipeline with models fitted.
        '''
        self.is_fitted = True
        stages = []
        for component in self.components:
            stages.append(component.model)
        self.spark_estimator_pipe = Pipeline(stages=stages)

        if dataset_path != None and 'ner' in self.nlu_ref:
            from sparknlp.training import CoNLL
            s_df = CoNLL().readDataset(self.spark,path=dataset_path, )
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(s_df.withColumnRenamed('label','y'))

        elif dataset_path != None and 'pos' in self.nlu_ref:
            from sparknlp.training import POS
            s_df = POS().readDataset(self.spark,path=dataset_path,delimiter=label_seperator,outputPosCol="y",outputDocumentCol="document",outputTextCol="text")
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(s_df)
        elif isinstance(dataset,pd.DataFrame) and 'multi' in  self.nlu_ref:
            schema = StructType([
                StructField("y", StringType(), True),
                StructField("text", StringType(), True)
                ])
            from pyspark.sql import functions as F
            df = self.spark.createDataFrame(data=dataset).withColumn('y',F.split('y',label_seperator))
            # df = self.spark.createDataFrame(data=dataset, schema=schema).withColumn('y',F.split('y',label_seperator))
            # df = self.spark.createDataFrame(dataset)
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(df)

        elif isinstance(dataset,pd.DataFrame):
            if not self.verify_all_labels_exist(dataset) : return nlu.NluError()
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(self.convert_pd_dataframe_to_spark(dataset))

        elif isinstance(dataset,pd.DataFrame) :
            if not self.verify_all_labels_exist(dataset) : return nlu.NluError()
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(self.convert_pd_dataframe_to_spark(dataset))

        else :
            # fit on empty dataframe since no data provided
            logger.info('Fitting on empty Dataframe, could not infer correct training method. This is intended for non-trainable pipelines.')
            self.spark_transformer_pipe = self.spark_estimator_pipe.fit(self.get_sample_spark_dataframe())


        return self
    # def convert_pd_dataframe_to_spark(self, data):
    #     #optimize
    #     return self.spark.createDataFrame(data)
    # def get_output_level_of_embeddings_provider(self, field_type, field_name):
    #     '''
    #     This function will go through all components to find the component which  generate @component_output_column_name.
    #     Then it will go gain through all components to find the component, from which @component_output_column_name is taking its inputs
    #     Then it will return the type of the provider component. This result isused to resolve the output level of the component that depends on the inpit for the output level
    #     :param field_type: The type of the field we want to resolve the input level for
    #     :param field_name: The name of the field we want to resolve the input level for
    #
    #     :return:
    #     '''
    #     # find the component. Column output name should be unique
    #     component_inputs = []
    #     for component in self.components:
    #         if field_name == component.info.name:
    #             component_inputs = component.info.spark_input_column_names
    #
    #     # get the embedding feature name
    #     target_output_component = ''
    #     for input_name in component_inputs:
    #         if 'embed' in input_name: target_output_component = input_name
    #
    #     # get the model that outputs that feature
    #     for component in self.components:
    #         component_outputs = component.info.spark_output_column_names
    #         for input_name in component_outputs:
    #             if target_output_component == input_name:
    #                 # this is the component that feeds into the component we are trying to resolve the output  level for.
    #                 # That is so, because the output of this component matches the input of the component we are resolving
    #                 return self.resolve_type_to_output_level(component.info.type)
    # def resolve_type_to_output_level(self, field_type, field_name):
    #     '''
    #     This checks the levels dict for what the output level is for the input annotator type.
    #     If the annotator type depends on the embedding level, we need further checking.
    #     @ param field_type : type of the spark field
    #     @ param name : name of thhe spark field
    #     @ return : String, which corrosponds to the output level of this Component.
    #     '''
    #     logger.info('Resolving output level for field_type=%s and field_name=%s', field_type, field_name)
    #     if field_name == 'sentence':
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to Sentence level', field_type,
    #                     field_name)
    #         return 'sentence'
    #     if field_type in self.levels['token']:
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to Token level ', field_type,
    #                     field_name)
    #         return 'token'
    #     if field_type in self.levels['sentence']:
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to sentence level', field_type,
    #                     field_name)
    #         return 'sentence'
    #     if field_type in self.levels['chunk']:
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to Chunk level ', field_type,
    #                     field_name)
    #         return 'chunk'
    #     if field_type in self.levels['document']:
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to document level', field_type,
    #                     field_name)
    #         return 'document'
    #     if field_type in self.levels['embedding_level']:
    #         logger.info('Resolved output level for field_type=%s and field_name=%s to embeddings level', field_type,
    #                     field_name)
    #         return self.get_output_level_of_embeddings_provider(field_type, field_name)  # recursive resolution
    # def resolve_input_dependent_component_to_output_level(self, component):
    #     '''
    #     For a given NLU component  which is input dependent , resolve its output level by checking if it's input stem from document or sentence based annotators
    #     :param component:  to resolve
    #     :return: resolve component
    #     '''
    #     # (1.) A classifier, which is using sentence/document. We just check input cols
    #
    #     if 'document' in component.info.spark_input_column_names :  return 'document'
    #     if 'sentence' in component.info.spark_input_column_names :  return 'sentence'
    #
    #     # (2.) A classifier, which is using sentence/doc embeddings.
    #     # We iterate over the pipe and check which Embed component is feeding the classifier and what the input that embed annotator is (sent or doc)
    #     for c in self.components:
    #         # check if c is of sentence embedding class  which is always input dependent
    #         if any ( isinstance(c.model, e ) for e in self.all_embeddings['input_dependent']  ) :
    #             if 'document' in c.info.spark_input_column_names :  return 'document'
    #             if 'sentence' in c.info.spark_input_column_names :  return 'sentence'
    # def resolve_component_to_output_level(self,component):
    #     '''
    #     For a given NLU component, resolve its output level, by checking annotator_levels dicts for approaches and models
    #     If output level is input dependent, resolve_input_dependent_component_to_output_level will resolve it
    #     :param component:  to resolve
    #     :return: resolve component
    #     '''
    #     for level in self.annotator_levels_model_based.keys():
    #         for t in self.annotator_levels_model_based[level]:
    #             if isinstance(component.model,t) :
    #                 if level == 'input_dependent' : return self.resolve_input_dependent_component_to_output_level(component)
    #                 else : return level
    #     for level in self.annotator_levels_approach_based.keys():
    #         for t in self.annotator_levels_approach_based[level]:
    #             if isinstance(component.model,t) :
    #                 if level == 'input_dependent' : return self.resolve_input_dependent_component_to_output_level(component)
    #                 else : return level
    #     if self.has_licensed_components:
    #         from  nlu.extractors.output_level_HC_map import HC_anno2output_level
    #         for level in HC_anno2output_level.keys():
    #             for t in HC_anno2output_level[level]:
    #                 if isinstance(component.model,t) :
    #                     if level == 'input_dependent' : return self.resolve_input_dependent_component_to_output_level(component)
    #                     else : return level
    # def get_output_level_mapping(self)->Dict[str,str]:
    #     """Get a dict where key=colname and val=output_level, inferred from processed dataframe and pipe that is currently running"""
    #     return {c.info.outputs[0] :self.resolve_component_to_output_level(c)  for c in self.components}
    # def get_cols_at_same_output_level(self,col2output_level:Dict[str,str])->List[str]:
    #     """Get List of cols which are at same output level as the pipe is currently configured to"""
    #     # return [c.info.outputs[0]  for c in self.components if self.resolve_component_to_output_level(c) == self.output_level ]
    #     return [c.info.outputs[0]  for c in self.components if col2output_level[c.info.outputs[0]] == self.output_level ]
    # def get_cols_not_at_same_output_level(self,col2output_level:Dict[str,str])->List[str]:
    #     """Get List of cols which are not at same output level as the pipe is currently configured to"""
    #     # return [c.info.outputs[0]  for c in self.components if not self.resolve_component_to_output_level(c) == self.output_level ]
    #     return [c.info.outputs[0]  for c in self.components if not col2output_level[c.info.outputs[0]] == self.output_level ]
    # def infer_and_set_output_level(self):
    #     '''
    #     This function checks the LAST  component of the NLU pipeline and infers
    #     and infers from that the output level via checking the components info.
    #     It sets the output level of the pipe accordingly
    #     param sdf : Spark dataframe after transformations
    #     '''
    #     if self.output_level == '' :
    #         # Loop in reverse over pipe and get first non util/sentence_detecotr/tokenizer/doc_assember. If there is non, take last
    #         bad_types = [ 'util','document','sentence']
    #         bad_names = ['token']
    #         for c in self.components[::-1]:
    #             if any (t in  c.info.type for t in bad_types) : continue
    #             if any (n in  c.info.name for n in bad_names) : continue
    #             self.output_level = self.resolve_component_to_output_level(c)
    #             logger.info(f'Inferred and set output level of pipeline to {self.output_level}', )
    #             break
    #         if self.output_level == None  or self.output_level == '': self.output_level = 'document' # Voodo Normalizer bug that does not happen in debugger bugfix
    #         logger.info(f'Inferred and set output level of pipeline to {self.output_level}' )
    #
    #     else : return


    def get_annotator_extraction_configs(self,full_meta):
        """Search first OC namespace and if not found the HC Namespace for each Annotator Class in pipeline and get corrosponding config
        Returns a dictionary of methods, where keys are column names values are methods  that are applied to extract and represent the data in these
        these columns in a more pythonic and panda-esque way
        """
        anno_2_ex_config = {}
        for c in self.components:
            if type(c.model) in OS_anno2config.keys():
                if OS_anno2config[type(c.model)]['default'] == '' or full_meta:
                    logger.info(f'could not find default configs, using full default for model ={c.model}')
                    anno_2_ex_config[c.info.spark_output_column_names[0]] = OS_anno2config[type(c.model)]['default_full'](output_col_prefix=c.info.outputs[0])
                else :
                    anno_2_ex_config[c.info.spark_output_column_names[0]] = OS_anno2config[type(c.model)]['default'](output_col_prefix=c.info.outputs[0])
            else:
                from nlu.extractors.extraction_resolver_HC import HC_anno2config
                if HC_anno2config[type(c.model)]['default'] == '' or full_meta :
                    logger.info(f'could not find default configs in hc resolver space, using full default for model ={c.model}')
                    anno_2_ex_config[c.info.spark_output_column_names[0]] = HC_anno2config[type(c.model)]['default_full'](output_col_prefix=c.info.outputs[0])
                else :
                    anno_2_ex_config[c.info.spark_output_column_names[0]] = HC_anno2config[type(c.model)]['default'](output_col_prefix=c.info.outputs[0])
        return anno_2_ex_config

    def unpack_and_apply_extractors(self,sdf:pyspark.sql.DataFrame, full_meta=False, keep_stranger_features=True, stranger_features=[])-> pd.DataFrame:
        # todo here user either Spark/Modin/Some other Backend
        """1. Unpack SDF to PDF with Spark NLP Annotator Dictionaries
           2. Get the extractor configs for the corrosponding Annotator classes
           3. Apply The extractor configs with the extractor methods to each column and merge back with zip/explode"""
        anno_2_ex_config = self.get_annotator_extraction_configs(full_meta,)
        unpack_df = sdf.toPandas().applymap(extract_pyspark_rows)

        return apply_extractors_and_merge(unpack_df,anno_2_ex_config, keep_stranger_features,stranger_features)





    def pythonify_spark_dataframe(self, processed,
                                  keep_stranger_features=True,
                                  stranger_features=[],
                                  drop_irrelevant_cols=True,
                                  output_metadata=False):

        '''
        This functions takes in a spark dataframe with Spark NLP annotations in it and transforms it into a Pandas Dataframe with common feature types for further NLP/NLU downstream tasks.
        It will recylce Indexes from Pandas DataFrames and Series if they exist, otherwise a custom id column will be created which is used as inex later on
            It does this by performing the following consecutive steps :
                1. Select columns to explode
                2. Select columns to keep
                3. Rename columns
                4. Create Pandas Dataframe object


        :param processed: Spark dataframe which an NLU pipeline has transformed
        :param output_level: The output level at which returned pandas Dataframe should be
        :param get_different_level_output:  Wheter to get features from different levels
        :param keep_stranger_features : Wether to keep additional features from the input DF when generating the output DF or if they should be discarded for the final output DF
        :param stranger_features: A list of features which are not known to NLU and inside of the input DF.
                                    Basically all columns, which are not named 'text' in the input.
                                    If keep_stranger_features== True, then these features will be exploded, if output_level == DOCUMENt, otherwise they will not be exploded
        :param output_metadata: Wether to keep or drop additional metadataf or predictions, like prediction confidence
        :return: Pandas dataframe which easy accessable features
        '''
        stranger_features += ['origin_index']

        # self.infer_and_set_output_level()
        if self.output_level == '' : self.output_level = OutputLevelUtils.infer_output_level(self)
        col2output_level                 = OutputLevelUtils.get_output_level_mapping(self)
        same_output_level                = OutputLevelUtils.get_cols_at_same_output_level(self,col2output_level)
        not_same_output_level            = OutputLevelUtils.get_cols_not_at_same_output_level(self,col2output_level)

        logger.info(f"Extracting for same_level_cols = {same_output_level}\nand different_output_level_cols = {not_same_output_level}")

        pretty_df = self.unpack_and_apply_extractors(processed, output_metadata, keep_stranger_features, stranger_features)
        pretty_df = zip_and_explode(pretty_df, same_output_level, not_same_output_level)
        pretty_df = self.convert_embeddings_to_np(pretty_df)
        if  drop_irrelevant_cols : return pretty_df[self.drop_irrelevant_cols(list(pretty_df.columns))]
        return pretty_df


# pretty_df =  self.finalize_retur_datatype(pretty_df)
















    def convert_embeddings_to_np(self, pdf):
        '''
        convert all the columns in a pandas df to numpy
        :param pdf: Pandas Dataframe whose embedding column will be converted to numpy array objects
        :return:
        '''

        for col in pdf.columns:
            if 'embed' in col:
                pdf[col] = pdf[col].apply(lambda x: np.array(x))
        return pdf


    def finalize_return_datatype(self, df):
        '''
        Take in a Spark dataframe with only relevant columns remaining.
        Depending on what value is set in self.output_datatype, this method will cast the final SDF into Pandas/Spark/Numpy/Modin/List objects
        :param df:
        :return: The predicted Data as datatype dependign on self.output_datatype
        '''

        if self.output_datatype == 'spark':
            return df # todo
        elif self.output_datatype == 'pandas':
            return df
        elif self.output_datatype == 'modin':
            import modin.pandas as mpd
            return mpd.DataFrame(df)
        elif self.output_datatype == 'pandas_series':
            return df
        elif self.output_datatype == 'modin_series':
            import modin.pandas as mpd
            return mpd.DataFrame(df)
        elif self.output_datatype == 'numpy':
            return df.to_numpy()
        return df


    def drop_irrelevant_cols(self, cols):
        '''
        Takes in a list of column names removes the elements which are irrelevant to the current output level.
        This will be run before returning the final df
        Drop column candidates are document, sentence, token, chunk.
        columns which are NOT AT THE SAME output level will be dropped
        :param cols:  list of column names in the df
        :return: list of columns with the irrelevant names removed
        '''
        if self.output_level == 'token':
            if 'document_results' in cols: cols.remove('document_results')
            if 'chunk_results' in cols: cols.remove('chunk_results')
            if 'sentence_results' in cols: cols.remove('sentence_results')
        if self.output_level == 'sentence':
            if 'token_results' in cols: cols.remove('token_results')
            if 'chunk_results' in cols: cols.remove('chunk_results')
            if 'document_results' in cols: cols.remove('document_results')
        if self.output_level == 'chunk':
            if 'document_results' in cols: cols.remove('document_results')
            if 'token_results' in cols: cols.remove('token_results')
            if 'sentence_results' in cols: cols.remove('sentence_results')
        if self.output_level == 'document':
            if 'token_results' in cols: cols.remove('token_results')
            if 'chunk_results' in cols: cols.remove('chunk_results')
            if 'sentence_results' in cols: cols.remove('sentence_results')
        if self.output_level == 'relation':
            if 'token_results' in cols: cols.remove('token_results')
            if 'chunk_results' in cols: cols.remove('chunk_results')
            if 'sentence_results' in cols: cols.remove('sentence_results')
        return cols


    def configure_light_pipe_usage(self, data_instances, use_multi=True):
        logger.info("Configuring Light Pipeline Usage")
        if data_instances > 50000 or use_multi == False:
            logger.info("Disabling light pipeline")
            self.fit()
            return
        else:
            if self.light_pipe_configured == False:
                self.light_pipe_configured = True
                logger.info("Enabling light pipeline")
                self.spark_transformer_pipe = LightPipeline(self.spark_transformer_pipe)

    def check_if_sentence_level_requirements_met(self):
        '''
        Check if the pipeline currently has an annotator that generate sentence col as output. If not, return False
        :return:
        '''

        for c in self.components:
            if 'sentence' in c.info.spark_output_column_names : return True
        return False

    def add_missing_sentence_component(self):
        '''
        Add Sentence Detector to pipeline and Run it thorugh the Query Verifiyer again.
        :return: None
        '''

    def write_nlu_pipe_info(self,path):
        '''
        Writes all information required to load a NLU pipeline from disk to path
        :param path: path where to store the nlu_info.json
        :return: True if success, False if failure
        '''
        import os
        f = open(os.path.join(path,'nlu_info.txt'), "w")
        f.write(self.nlu_ref)
        f.close()
        #1. Write all primitive pipe attributes to dict
        # pipe_data = {
        #     'has_trainable_components': self.has_trainable_components,
        #     'is_fitted' : self.is_fitted,
        #     'light_pipe_configured' : self.light_pipe_configured,
        #     'needs_fitting':self.needs_fitting,
        #     'nlu_reference':self.nlu_reference,
        #     'output_datatype':self.output_datatype,
        #     'output_different_levels':self.output_different_levels,
        #     'output_level': self.output_level,
        #     'output_positions': self.output_positions,
        #     'pipe_componments': {},
        #     'pipe_ready':self.pipe_ready,
        #     'provider': self.provider,
        #     'raw_text_column': self.raw_text_column,
        #     'raw_text_matrix_slice': self.raw_text_matrix_slice,
        #     'spark_nlp_pipe': self.spark_nlp_pipe,
        #     'spark_non_light_transformer_pipe': self.spark_non_light_transformer_pipe,
        #     'component_count': len(self)
        #
        # }

        #2. Write all component/component_info to dict
        # for c in self.pipe_components:
        #     pipe_data['pipe_componments'][c.ma,e]
        #3. Any additional stuff

        return True

    def add_missing_component_if_missing_for_output_level(self):
        '''
        Check that for currently configured self.output_level one annotator for that level exists, i.e a Sentence Detetor for outpul tevel sentence, Tokenizer for level token etc..

        :return: None
        '''

        if self.output_level =='sentence':
            if self.check_if_sentence_level_requirements_met(): return
            else :
                logger.info('Adding missing sentence Dependency because it is missing for outputlevel=Sentence')
                self.add_missing_sentence_component()
    def save(self, path, component='entire_pipeline', overwrite=False):

        if nlu.is_running_in_databricks() :
            if path.startswith('/dbfs/') or path.startswith('dbfs/'):
                nlu_path = path
                if path.startswith('/dbfs/'):
                    nlp_path =  path.replace('/dbfs','')
                else :
                    nlp_path =  path.replace('dbfs','')

            else :
                nlu_path = 'dbfs/' + path
                if path.startswith('/') : nlp_path = path
                else : nlp_path = '/' + path

            if not self.is_fitted and self.has_trainable_components:
                self.fit()
                self.is_fitted = True
            if component == 'entire_pipeline':
                self.spark_transformer_pipe.save(nlp_path)
                self.write_nlu_pipe_info(nlu_path)


        if overwrite and not nlu.is_running_in_databricks():
            import shutil
            shutil.rmtree(path,ignore_errors=True)


        if not self.is_fitted :
            self.fit()
            self.is_fitted = True
        if component == 'entire_pipeline':
            self.spark_transformer_pipe.save(path)
            self.write_nlu_pipe_info(path)
        else:
            if component in self.keys():
                self[component].save(path)
            # else :
            #     print(f"Error during saving,{component} does not exist in the pipeline.\nPlease use pipe.print_info() to see the references you need to pass save()")

        print(f'Stored model in {path}')
        # else : print('Please fit untrained pipeline first or predict on a String to save it')
    def predict(self, data, output_level='', positions=False, keep_stranger_features=True, metadata=False,
                multithread=True, drop_irrelevant_cols=True, verbose=False, return_spark_df = False):
        '''
        Annotates a Pandas Dataframe/Pandas Series/Numpy Array/Spark DataFrame/Python List strings /Python String

        :param data: Data to predict on
        :param output_level: output level, either document/sentence/chunk/token
        :param positions: wether to output indexes that map predictions back to position in origin string
        :param keep_stranger_features: wether to keep columns in the dataframe that are not generated by pandas. I.e. when you s a dataframe with 10 columns and only one of them is named text, the returned dataframe will only contain the text column when set to false
        :param metadata: wether to keep additonal metadata in final df or not like confidiences of every possible class for preidctions.
        :param multithread: Whether to use multithreading based lightpipeline. In some cases, this may cause errors.
        :param drop_irellevant_cols: Wether to drop cols of different output levels, i.e. when predicting token level and dro_irrelevant_cols = True then chunk, sentence and Doc will be dropped
        :param return_spark_df: Prediction results will be returned right after transforming with the Spark NLP pipeline
        :return:
        '''

        if output_level != '': self.output_level = output_level

        self.output_positions = positions

        # if output_level == 'chunk':
        #     # If no chunk output component in pipe we must add it and run the query PipelineQueryVerifier again
        #     chunk_provided = False
        #     for component in self.components:
        #         if component.info.output_level == 'chunk': chunk_provided = True
        #     if chunk_provided == False:
        #         self.components.append(nlu.pipe.component_resolution.get_default_component_of_type('chunk'))
        #         # this could break indexing..
        #
        #         self = nlu.pipe.pipeline_logic.PipelineQueryVerifier.check_and_fix_nlu_pipeline(self)
        # if not self.is_fitted: self.fit()

        # currently have to always fit, otherwise parameter changes wont take effect
        # if output_level == 'sentence' or output_level == 'document':
            # self = PipeUtils.configure_component_output_levels(self)
            # self = PipeUtils.check_and_fix_nlu_pipeline(self)
            # 1 # todo

        if not self.is_fitted :
            if self.has_trainable_components :
                self.fit(data)
            else : self.fit()
        # self.configure_light_pipe_usage(len(data), multithread)

        sdf = None
        stranger_features = []
        index_provided = False
        infered_text_col = False

        try:
            if isinstance(data,pyspark.sql.dataframe.DataFrame):  # casting follows spark->pd
                self.output_datatype = 'spark'
                data = data.withColumn('origin_index',monotonically_increasing_id().alias('origin_index'))
                index_provided = True

                if self.raw_text_column in data.columns:
                    # store all stranger features
                    if len(data.columns) > 1:
                        stranger_features = list(set(data.columns) - set(self.raw_text_column))
                    sdf = self.spark_transformer_pipe.transform(data)
                else:
                    print(
                        'Could not find column named "text" in input Pandas Dataframe. Please ensure one column named such exists. Columns in DF are : ',
                        data.columns)
            elif isinstance(data,pd.DataFrame):  # casting follows pd->spark->pd
                self.output_datatype = 'pandas'

                # set first col as text column if there is none
                if self.raw_text_column not in data.columns:
                    data.rename(columns={data.columns[0]: 'text'}, inplace=True)
                data['origin_index'] = data.index
                index_provided = True
                if self.raw_text_column in data.columns:
                    if len(data.columns) > 1:
                        data = data.where(pd.notnull(data), None)  # make  Nans to None, or spark will crash
                        data = data.dropna(axis=1, how='all')
                        stranger_features = list(set(data.columns) - set(self.raw_text_column))
                    sdf = self.spark_transformer_pipe.transform(self.spark.createDataFrame(data))

                else:
                    logger.info(
                        'Could not find column named "text" in input Pandas Dataframe. Please ensure one column named such exists. Columns in DF are : ',
                        data.columns)
            elif isinstance(data,pd.Series):  # for df['text'] colum/series passing casting follows pseries->pdf->spark->pd
                self.output_datatype = 'pandas_series'
                data = pd.DataFrame(data).dropna(axis=1, how='all')
                index_provided = True
                # If series from a column is passed, its column name will be reused.
                if self.raw_text_column not in data.columns and len(data.columns) == 1:
                    data['text'] = data[data.columns[0]]
                else:
                    logger.info(f'INFO: NLU will assume {data.columns[0]} as label column since default text column could not be find')
                    data['text'] = data[data.columns[0]]

                data['origin_index'] = data.index

                if self.raw_text_column in data.columns:
                    sdf = self.spark_transformer_pipe.transform(self.spark.createDataFrame(data), )

                else:
                    print(
                        'Could not find column named "text" in  Pandas Dataframe generated from input  Pandas Series. Please ensure one column named such exists. Columns in DF are : ',
                        data.columns)

            elif isinstance(data,np.ndarray):
                # This is a bit inefficient. Casting follow  np->pd->spark->pd. We could cut out the first pd step
                self.output_datatype = 'numpy_array'
                if len(data.shape) != 1:
                    print("Exception : Input numpy array must be 1 Dimensional for prediction.. Input data shape is",
                          data.shape)
                    return nlu.NluError
                sdf = self.spark_transformer_pipe.transform(self.spark.createDataFrame(
                    pd.DataFrame({self.raw_text_column: data, 'origin_index': list(range(len(data)))})))
                index_provided = True

            elif isinstance(data,np.matrix):  # assumes default axis for raw texts
                print(
                    'Predicting on np matrices currently not supported. Please input either a Pandas Dataframe with a string column named "text"  or a String or a list of strings. ')
                return nlu.NluError
            elif isinstance(data,str):  # inefficient, str->pd->spark->pd , we can could first pd
                self.output_datatype = 'string'
                sdf = self.spark_transformer_pipe.transform(self.spark.createDataFrame(
                    pd.DataFrame({self.raw_text_column: data, 'origin_index': [0]}, index=[0])))
                index_provided = True

            elif isinstance(data,list):  # inefficient, list->pd->spark->pd , we can could first pd
                self.output_datatype = 'string_list'
                if all(type(elem) == str for elem in data):
                    sdf = self.spark_transformer_pipe.transform(self.spark.createDataFrame(pd.DataFrame(
                        {self.raw_text_column: pd.Series(data), 'origin_index': list(range(len(data)))})))
                    index_provided = True

                else:
                    print("Exception: Not all elements in input list are of type string.")
            elif isinstance(data,dict):  # Assumes values should be predicted
                print(
                    'Predicting on dictionaries currently not supported. Please input either a Pandas Dataframe with a string column named "text"  or a String or a list of strings. ')
                return ''
            else:  # Modin tests, This could crash if Modin not installed
                try:
                    import modin.pandas as mpd
                    if isinstance(data, mpd.DataFrame):
                        data = pd.DataFrame(data.to_dict())  # create pandas to support type inference
                        self.output_datatype = 'modin'
                        data['origin_index'] = data.index
                        index_provided = True

                    if self.raw_text_column in data.columns:
                        if len(data.columns) > 1:
                            data = data.where(pd.notnull(data), None)  # make  Nans to None, or spark will crash
                            data = data.dropna(axis=1, how='all')
                            stranger_features = list(set(data.columns) - set(self.raw_text_column))
                        sdf = self.spark_transformer_pipe.transform(
                            # self.spark.createDataFrame(data[['text']]), ) # this takes text column as series and makes it DF
                            self.spark.createDataFrame(data))
                    else:
                        print(
                            'Could not find column named "text" in input Pandas Dataframe. Please ensure one column named such exists. Columns in DF are : ',
                            data.columns)

                    if isinstance(data, mpd.Series):
                        self.output_datatype = 'modin_series'
                        data = pd.Series(data.to_dict())  # create pandas to support type inference
                        data = pd.DataFrame(data).dropna(axis=1, how='all')
                        data['origin_index'] = data.index
                        index_provided = True
                        if self.raw_text_column in data.columns:
                            sdf = \
                                self.spark_transformer_pipe.transform(
                                    self.spark.createDataFrame(data[['text']]), )
                        else:
                            print(
                                'Could not find column named "text" in  Pandas Dataframe generated from input  Pandas Series. Please ensure one column named such exists. Columns in DF are : ',
                                data.columns)


                except:
                    print(
                        "If you use Modin, make sure you have installed 'pip install modin[ray]' or 'pip install modin[dask]' backend for Modin ")

            if return_spark_df : return sdf  # Returns RAW result of pipe prediction
            return self.pythonify_spark_dataframe(sdf,
                                                  keep_stranger_features=keep_stranger_features,
                                                  stranger_features=stranger_features,
                                                  output_metadata=metadata,
                                                  drop_irrelevant_cols=drop_irrelevant_cols
                                                  )


        except Exception as err :
            import sys
            if multithread == True:
                logger.warning("Multithreaded mode failed. trying to predict again with non multithreaded mode ")
                return self.predict(data, output_level=output_level, positions=positions,
                                    keep_stranger_features=keep_stranger_features, metadata=metadata, multithread=False)
            logger.exception('Exception occured')
            e = sys.exc_info()
            print("No accepted Data type or usable columns found or applying the NLU models failed. ")
            print(
                "Make sure that the first column you pass to .predict() is the one that nlu should predict on OR rename the column you want to predict on to 'text'  ")
            print(
                "If you are on Google Collab, click on Run time and try factory reset Runtime run the setup script again, you might have used too much memory")
            print(
                "On Kaggle try to reset restart session and run the setup script again, you might have used too much memory")

            print('Full Stacktrace was', e)
            print('Additional info:')
            exc_type, exc_obj, exc_tb = sys.exc_info()
            import os
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            print(
                'Stuck? Contact us on Slack! https://join.slack.com/t/spark-nlp/shared_invite/zt-j5ttxh0z-Fn3lQSG1Z0KpOs_SRxjdyw0196BQCDPY')
            if verbose :
                err = sys.exc_info()[1]
                print(str(err))
            return None


    def print_info(self, ):
        '''
        Print out information about every component currently loaded in the pipe and their configurable parameters
        :return: None
        '''

        print('The following parameters are configurable for this NLU pipeline (You can copy paste the examples) :')
        # list of tuples, where first element is component name and second element is list of param tuples, all ready formatted for printing
        all_outputs = []

        for i, component_key in enumerate(self.keys()):
            s = ">>> pipe['" + component_key + "'] has settable params:"
            p_map = self[component_key].extractParamMap()

            component_outputs = []
            max_len = 0
            for key in p_map.keys():
                if "outputCol" in key.name or "labelCol" in key.name or "inputCol" in key.name or "labelCol" in key.name or 'lazyAnnotator' in key.name or 'storageref' in key.name: continue
                # print("pipe['"+ component_key +"'].set"+ str( key.name[0].capitalize())+ key.name[1:]+"("+str(p_map[key])+")" + " | Info: " + str(key.doc)+ " currently Configured as : "+str(p_map[key]) )
                # print("Param Info: " + str(key.doc)+ " currently Configured as : "+str(p_map[key]) )

                if type(p_map[key]) == str:
                    s1 = "pipe['" + component_key + "'].set" + str(key.name[0].capitalize()) + key.name[
                                                                                               1:] + "('" + str(
                        p_map[key]) + "') "
                else:
                    s1 = "pipe['" + component_key + "'].set" + str(key.name[0].capitalize()) + key.name[1:] + "(" + str(
                        p_map[key]) + ") "

                s2 = " | Info: " + str(key.doc) + " | Currently set to : " + str(p_map[key])
                if len(s1) > max_len: max_len = len(s1)
                component_outputs.append((s1, s2))

            all_outputs.append((s, component_outputs))

        # make strings aligned
        form = "{:<" + str(max_len) + "}"
        for o in all_outputs:
            print(o[0])  # component name
            for o_parm in o[1]:
                if len(o_parm[0]) < max_len:
                    print(form.format(o_parm[0]) + o_parm[1])
                else:
                    print(o_parm[0] + o_parm[1])

