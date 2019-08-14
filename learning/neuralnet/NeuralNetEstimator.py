import sys
import shutil

import tensorflow as tf
from tensorflow.python.summary import summary
from privacy.analysis import privacy_ledger
from privacy.analysis.rdp_accountant import compute_rdp_from_ledger, get_privacy_spent
from privacy.optimizers import dp_optimizer


class CheckPrivacyBudgetHook(tf.estimator.SessionRunHook):
    def __init__(self, ledger):
        self._samples, self._queries = ledger.get_unformatted_ledger()

    def end(self, session):
        orders = [1 + x / 10.0 for x in range(1, 100)] + list(range(12, 64))
        samples = session.run(self._samples)
        queries = session.run(self._queries)
        formatted_ledger = privacy_ledger.format_ledger(samples, queries)
        rdp = compute_rdp_from_ledger(formatted_ledger, orders)
        target_delta = 2e-4
        eps = get_privacy_spent(orders, rdp, target_delta=target_delta)[0]
        print('For delta={:.5}, the current epsilon is: {:.5}'.format(target_delta, eps))


class NeuralNetEstimator:

    def __init__(self, feature_columns, flags, training_samples_count):
        self.feature_columns = feature_columns
        self.flags = flags
        self.estimator = None

        # DP necessary members
        self.training_samples_count = training_samples_count
        self.q = self.flags.batch_size * 1.0 / self.training_samples_count
        self.rdp_orders = [1 + x / 10. for x in range(1, 100)] + list(range(12, 64))
        return

    def _add_hidden_layer_summary(self, value, tag):
        summary.scalar('%s/fraction_of_zero_values' % tag, tf.nn.zero_fraction(value))
        summary.histogram('%s/activation' % tag, value)

    def _dense_batch_relu(self, input, num_nodes, phase, layer_name, batchnorm, dropout):
        if batchnorm:
            out = tf.layers.dense(input, num_nodes, activation=tf.nn.relu, name=layer_name)
            out = tf.layers.batch_normalization(out, training=phase)
        else:
            out = tf.layers.dense(input, num_nodes, activation=tf.nn.relu, name=layer_name)

        if dropout is not None:
            out = tf.layers.dropout(out, rate=dropout, training=phase)
        return out

    def _dense_batchnorm_fn(self, features, labels, mode, params):
        """Model function for Estimator."""
        hidden_units = params['hidden_units']
        dropout = params['dropout']
        batchnorm = params['batchnorm']

        input_layer = tf.feature_column.input_layer(features, params['feature_columns'])
        for l_id, num_units in enumerate(hidden_units):
            l_name = 'hiddenlayer_%d' % l_id
            l = self._dense_batch_relu(input_layer, num_units, mode == tf.estimator.ModeKeys.TRAIN, l_name, batchnorm,
                                       dropout)
            self._add_hidden_layer_summary(l, l_name)
            input_layer = l

        if batchnorm:
            logits = tf.layers.dense(input_layer, 2, activation=None, name='logits')
            logits = tf.layers.batch_normalization(logits, training=(mode == tf.estimator.ModeKeys.TRAIN))
        else:
            logits = tf.layers.dense(input_layer, 2, activation=None, name='logits')
        self._add_hidden_layer_summary(logits, 'logits')

        # Reshape output layer to 1-dim Tensor to return predictions
        probabilities = tf.nn.softmax(logits)
        predictions = tf.round(probabilities)
        predicted = tf.argmax(predictions, axis=1)
        # Provide an estimator spec for `ModeKeys.PREDICT`.
        if mode == tf.estimator.ModeKeys.PREDICT:
            return tf.estimator.EstimatorSpec(
                mode=mode,
                export_outputs={'predict_output': tf.estimator.export.PredictOutput({"Wiederkehrer": predictions,
                                                                                     'probabilities': probabilities})},
                predictions={
                    'Wiederkehrer': predictions,
                    'logits': logits,
                    'probabilities': probabilities
                })

        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=tf.cast(labels, tf.int32), logits=logits)
        scalar_loss = tf.reduce_mean(cross_entropy)

        if self.flags.enable_dp:
            training_loss = cross_entropy
        else:
            training_loss = scalar_loss

        # Compute evaluation metrics.
        # mean_squared_error = tf.metrics.mean_squared_error(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predictions, tf.float64), name='mean_squared_error')
        accuracy = tf.metrics.accuracy(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                       name='accuracy')
        precision = tf.metrics.precision(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                         name='precision')
        recall = tf.metrics.recall(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                   name='recall')
        auc = tf.metrics.auc(labels=tf.cast(labels, tf.float64), predictions=probabilities[:, 1], name='auc')
        fp = tf.metrics.false_positives(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                        name='false_positives')
        tp = tf.metrics.true_positives(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                       name='true_positives')
        fn = tf.metrics.false_negatives(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                        name='false_negatives')
        tn = tf.metrics.true_negatives(labels=tf.cast(labels, tf.float64), predictions=tf.cast(predicted, tf.float64),
                                       name='false_negatives')

        tf.summary.scalar('accuracy', accuracy[1])

        if mode == tf.estimator.ModeKeys.EVAL:
            avg_loss = tf.reduce_mean(cross_entropy)
            tf.summary.scalar('avg_loss', avg_loss)

        # Calculate root mean squared error as additional eval metric
        eval_metric_ops = {'accuracy': accuracy,
                           'precision': precision,
                           'recall': recall,
                           'auc': auc,
                           'true positives': tp,
                           'true negatives': tn,
                           'false positives': fp,
                           'false negatives': fn
                           }

        global_step = tf.train.get_global_step()
        starter_learning_rate = params['learning_rate']
        learning_rate = tf.train.exponential_decay(starter_learning_rate, global_step, 1000000, 0.96, staircase=True)

        # If differential privacy is enabled, use it
        if self.flags.enable_dp:
            ledger = privacy_ledger.PrivacyLedger(population_size=self.training_samples_count,
                                                  selection_probability=self.q)
            optimizer = dp_optimizer.DPAdamGaussianOptimizer(learning_rate=learning_rate, l2_norm_clip=self.flags.dp_c,
                                                             noise_multiplier=self.flags.dp_sigma,
                                                             num_microbatches=self.flags.dp_num_microbatches,
                                                             ledger=ledger)
            training_hooks = [CheckPrivacyBudgetHook(ledger)]

        # Otherwise just use a normal ADAMOptimizer
        else:
            optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
            training_hooks = None

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss=training_loss, global_step=global_step)

        # Provide an estimator spec for `ModeKeys.EVAL` and `ModeKeys.TRAIN` modes.
        return tf.estimator.EstimatorSpec(
            mode=mode,
            loss=scalar_loss,
            train_op=train_op,
            eval_metric_ops=eval_metric_ops,
            training_hooks=training_hooks,
            evaluation_hooks=None)

    def _build_estimator(self):
        tf.reset_default_graph()
        """Build an estimator appropriate for the given model type."""

        deep_columns = self.feature_columns.buildModelColumns()

        # Create a tf.estimator.RunConfig to ensure the model is run on CPU, which
        # trains faster than GPU for this model.
        run_config = tf.estimator.RunConfig().replace(
            session_config=tf.ConfigProto(device_count={'GPU': 0}))

        # warm start settings:
        ws = None
        if self.flags.continue_training:
            # like that: all weights (input layer and hidden weights are warmstarted)
            if self.flags.pretrained_model_dir is not None:
                # os.system('scp -r ' + self.flags.pretrained_model_dir + ' ' + self.flags.model_dir + '/')
                ws = tf.estimator.WarmStartSettings(ckpt_to_initialize_from=self.flags.pretrained_model_dir,
                                                    var_name_to_prev_var_name=self.feature_columns.getConversionDict()
                                                    )
            else:
                print('continue_training flag is set to True, but not pretrained_model_dir_specified...exit')
                sys.exit()

        params_batchnorm = {'feature_columns': deep_columns,
                            'hidden_units': self.flags.hidden_units,
                            'batchnorm': self.flags.batchnorm,
                            'dropout': self.flags.dropout,
                            'learning_rate': self.flags.learningrate}

        self.estimator = tf.estimator.Estimator(
            model_fn=self._dense_batchnorm_fn,
            model_dir=self.flags.model_dir,
            params=params_batchnorm,
            config=run_config,
            warm_start_from=ws
        )

    def getEstimator(self):
        if self.estimator is None:
            self._build_estimator()
        return self.estimator
