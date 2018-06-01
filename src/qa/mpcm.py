import sys
sys.path.insert(0, "/Users/tom/Dropbox/msc-ml/project/src/")


import tensorflow as tf

from base_model import TFModel

import helpers.loader as loader


import flags
FLAGS = tf.app.flags.FLAGS

mem_limit=0.25


# This should handle the mechanics of the model - basically it's a wrapper around the TF graph
class MpcmQa(TFModel):
    def __init__(self, vocab):
        self.embedding_size = tf.app.flags.FLAGS.embedding_size
        self.vocab = vocab
        super().__init__()


    def build_model(self):

        # TODO: dropout

        self.context_in = tf.placeholder(tf.int32, [None, None])
        self.question_in = tf.placeholder(tf.int32, [None, None])

        self.answer_spans_in = tf.placeholder(tf.int32, [None, 2])

        self.embeddings = tf.get_variable('word_embeddings', [len(self.vocab), self.embedding_size], initializer=tf.orthogonal_initializer)

        # Load glove embeddings
        self.glove_init_ops =[]
        glove_embeddings = loader.load_glove(FLAGS.data_path, d=FLAGS.embedding_size)
        for word,id in self.vocab.items():
            if word in glove_embeddings.keys():
                self.glove_init_ops.append(tf.assign(self.embeddings[id,:], glove_embeddings[word]))

        # Layer 1: representation layer
        self.context_embedded = tf.layers.dropout(tf.nn.embedding_lookup(self.embeddings, self.context_in), rate=0.2, training=self.is_training)
        self.question_embedded = tf.layers.dropout(tf.nn.embedding_lookup(self.embeddings, self.question_in), rate=0.2, training=self.is_training)

        # Layer 2: Filter. r is batch x con_len x q_len
        # tf.einsum("bid,bjd->bij",self.context_embedded, self.question_embedded)
        #tf.einsum("bi,bj->bij", tf.norm(self.context_embedded, ord=1, axis=2),tf.norm(self.question_embedded, ord=1, axis=2))
        r = tf.matmul(self.context_embedded, tf.transpose(self.question_embedded,[0,2,1]))/tf.matmul(tf.expand_dims(tf.norm(self.context_embedded, ord=1, axis=2),-1),tf.expand_dims(tf.norm(self.question_embedded, ord=1, axis=2),-2))
        r_context = tf.reduce_max(r, axis=2, keep_dims=True)
        r_question = tf.reduce_max(r, axis=1, keep_dims=True)

        self.context_filtered = tf.layers.dropout(tf.tile(r_context, [1,1,self.embedding_size]) * self.context_embedded, rate=0.2, training=self.is_training)
        self.question_filtered = tf.layers.dropout(tf.tile(tf.transpose(r_question,[0,2,1]), [1,1,self.embedding_size]) * self.question_embedded, rate=0.2, training=self.is_training)

        # print(self.context_filtered)
        # print(self.question_filtered)

        # Layer 3: Context representation (BiLSTM encoder)
        num_units_encoder=100
        cell_fw = tf.contrib.rnn.BasicLSTMCell(num_units=num_units_encoder, name="layer3_fwd_cell")
        cell_bw = tf.contrib.rnn.BasicLSTMCell(num_units=num_units_encoder, name="layer3_bwd_cell")
        self.context_encodings,_ = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, self.context_filtered, dtype=tf.float32)
        self.question_encodings,_ = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, self.question_filtered, dtype=tf.float32)

        # print(self.context_encodings)
        # print(self.question_encodings)

        # Layer 4: context matching layer
        def similarity(v1, v2, W): #v1,v2 are batch x seq x d, W is lxd
            W_tiled = tf.tile(tf.expand_dims(W,axis=-1), [1,1,tf.shape(W)[1]])
            # print(W_tiled)
            v1_weighted =tf.tensordot(v1, W_tiled, [[-1],[-1]])
            # print(v1_weighted)
            v2_weighted =tf.tensordot(v2, W_tiled, [[-1],[-1]])
            # print(v2_weighted)
            similarity = tf.einsum("bild,bjld->bijl", v1_weighted, v2_weighted)
            # print(similarity)
            return similarity

        m_fwd = tf.layers.dropout(similarity(self.context_encodings[0], self.question_encodings[0], tf.get_variable("W1", (50, num_units_encoder), tf.float32, tf.random_uniform_initializer(-1,1))), rate=0.2, training=self.is_training)
        m_bwd = tf.layers.dropout(similarity(self.context_encodings[1], self.question_encodings[1], tf.get_variable("W2", (50, num_units_encoder), tf.float32, tf.random_uniform_initializer(-1,1))), rate=0.2, training=self.is_training)

        m_full_fwd = m_fwd[:,:,-1,:]
        m_full_bwd = m_bwd[:,:,0,:]
        m_max_fwd  = tf.reduce_max(m_fwd, axis=2)
        m_max_bwd  = tf.reduce_max(m_bwd, axis=2)
        m_mean_fwd  = tf.reduce_mean(m_fwd, axis=2)
        m_mean_bwd  = tf.reduce_mean(m_bwd, axis=2)
        self.matches = tf.concat([m_full_fwd, m_full_bwd, m_max_fwd, m_max_bwd, m_mean_fwd, m_mean_bwd], axis=2)

        # print(m_full_bwd)
        # print(self.matches)

        # Layer 5: aggregate with BiLSTM
        cell_fw2 = tf.contrib.rnn.BasicLSTMCell(num_units=100, name="layer5_fwd_cell")
        cell_bw2 = tf.contrib.rnn.BasicLSTMCell(num_units=100, name="layer5_bwd_cell")
        self.aggregated_matches,_ = tf.nn.bidirectional_dynamic_rnn(cell_fw2, cell_bw2, self.matches, dtype=tf.float32)
        self.aggregated_matches = tf.layers.dropout(tf.concat(self.aggregated_matches, axis=2), rate=0.2, training=self.is_training)

        # Layer 6: Fully connected to get logits
        self.logits_start = tf.squeeze(tf.layers.dense(self.aggregated_matches, 1, activation=None))
        self.logits_end = tf.squeeze(tf.layers.dense(self.aggregated_matches, 1, activation=None))

        self.prob_start = tf.nn.softmax(self.logits_start, axis=-1)
        self.prob_end = tf.nn.softmax(self.logits_end, axis=-1)

        # training loss
        self.loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.answer_spans_in[:,0], logits=self.logits_start)+tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.answer_spans_in[:,1], logits=self.logits_end))
        self.optimise = tf.train.AdamOptimizer(1e-4).minimize(self.loss)

        self.accuracy = tf.reduce_mean(tf.cast(tf.equal(tf.stack([tf.argmax(self.prob_start, axis=-1, output_type=tf.int32),tf.argmax(self.prob_end, axis=-1, output_type=tf.int32)],axis=1), self.answer_spans_in), tf.float32))

        # predictions: coerce start<end