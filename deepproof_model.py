import math
from keras.models import Model
from keras.layers import Input, LSTM, CuDNNLSTM, Dense, Embedding, Reshape, Concatenate, Lambda, Conv1D, Multiply, Bidirectional, MaxPooling1D, Activation
from keras import backend as K
import numpy as np
import h5py
import sys
import encoding
from multihead import MultiHead
from attention import Attention

embed_dim = 64
latent_dim = 512  # Latent dimensionality of the encoding space.
attn_dim = 128
num_encoder_tokens = len(encoding.char_list)

def compute_attention_weights(inputs):
    x, y = inputs
    output = K.batch_dot(x, y, axes=[2,2])
    return output

def apply_attention_weights(inputs):
    x, y = inputs
    output = K.batch_dot(x, y)
    return output

def create(use_gpu):
    # Define an input sequence and process it.
    encoder_inputs = Input(shape=(None, 1))
    reshape1 = Reshape((-1, embed_dim))
    reshape2 = Reshape((-1, embed_dim))
    conv1a = Conv1D(latent_dim, 11, padding='same', activation='tanh')
    conv1b = Conv1D(latent_dim, 11, padding='same', activation='sigmoid')
    embed = Embedding(num_encoder_tokens, embed_dim)
    if use_gpu:
        encoder = CuDNNLSTM(latent_dim, return_sequences=True, return_state=True)
        encoder2 = CuDNNLSTM(latent_dim//2, return_sequences=True)
    else:
        encoder = LSTM(latent_dim, recurrent_activation="sigmoid", return_sequences=True, return_state=True)
        encoder2 = LSTM(latent_dim//2, recurrent_activation="sigmoid", return_sequences=True)
    encoder = Bidirectional(encoder, merge_mode='concat')
    encoder2 = Bidirectional(encoder2, merge_mode='concat')
    emb = reshape1(embed(encoder_inputs));
    c1a = conv1a(emb)
    c1b = conv1b(emb)
    encoder_outputs, state_h, state_c, _, _ = encoder(Multiply()([c1a, c1b]))
    rev = Lambda(lambda x: K.reverse(x, 1))
    conv2 = Conv1D(latent_dim, 5, dilation_rate=2, padding='same', activation='tanh')

    encoder_outputs = MaxPooling1D()(encoder_outputs)
    encoder_outputs = MaxPooling1D()(encoder2(encoder_outputs))
    #encoder_outputs = conv2(rev(encoder_outputs))
    encoder_states = [state_h, state_c]

    # Set up the decoder, using `encoder_states` as initial state.
    decoder_inputs = Input(shape=(None, 1))
    # We set up our decoder to return full output sequences,
    # and to return internal states as well. We don't use the
    # return states in the training model, but we will use them in inference.
    if use_gpu:
        language_lstm = CuDNNLSTM(latent_dim, return_sequences=True, return_state=True)
        decoder_lstm = CuDNNLSTM(latent_dim, return_sequences=True, return_state=True)
    else:
        language_lstm = LSTM(latent_dim, recurrent_activation="sigmoid", return_sequences=True, return_state=True)
        decoder_lstm = LSTM(latent_dim, recurrent_activation="sigmoid", return_sequences=True, return_state=True)

    dec_lstm_input = reshape1(embed(decoder_inputs))

    language_outputs, _, _ = language_lstm(dec_lstm_input)

    attn = Attention(attn_dim, activation='tanh')
    attn_output = attn([language_outputs, encoder_outputs, encoder_outputs])
    
    dec_lstm_input2 = Concatenate()([dec_lstm_input, language_outputs, attn_output])

    decoder_outputs, _, _ = decoder_lstm(dec_lstm_input2,
                                         initial_state=encoder_states)
    decoder_dense = Dense(num_encoder_tokens, activation='softmax')
    decoder_outputs = decoder_dense(decoder_outputs)

    model = Model([encoder_inputs, decoder_inputs], decoder_outputs)

    #The following is needed for inference (one at a time decoding) only
    encoder_model = Model(encoder_inputs, [encoder_outputs, state_h, state_c])

    decoder_state_input_h = Input(shape=(latent_dim,))
    decoder_state_input_c = Input(shape=(latent_dim,))
    lang_state_input_h = Input(shape=(latent_dim,))
    lang_state_input_c = Input(shape=(latent_dim,))
    decoder_states_inputs = [decoder_state_input_h, decoder_state_input_c, lang_state_input_h, lang_state_input_c]
    decoder_enc_inputs = Input(shape=(None, latent_dim))
    first_decoder_enc_inputs = Input(shape=(None, 2*latent_dim))
    tmp = reshape1(embed(decoder_inputs))
    lang_outputs, lstate_h, lstate_c = language_lstm(tmp, initial_state=decoder_states_inputs[2:])

    attn_output = attn([lang_outputs, decoder_enc_inputs, decoder_enc_inputs])

    decoder_outputs, state_h, state_c = decoder_lstm(
        Concatenate()([tmp, lang_outputs, attn_output]), initial_state=decoder_states_inputs[0:2])
    decoder_states = [state_h, state_c, lstate_h, lstate_c]
    decoder_outputs = decoder_dense(decoder_outputs)
    decoder_model = Model(
        [decoder_inputs, decoder_enc_inputs, first_decoder_enc_inputs] + decoder_states_inputs,
        [decoder_outputs] + decoder_states)
    return (encoder_model, decoder_model, model)

def decode_sequence(models, input_seq):
    [encoder_model, decoder_model] = models
    # Encode the input as state vectors.
    encoder_outputs, state_h, state_c = encoder_model.predict(input_seq[:,:,0:1])
    lstate_h = lstate_c = np.zeros((1, latent_dim))
    states_value = [state_h, state_c, lstate_h, lstate_c]

    # Generate empty target sequence of length 1.
    target_seq = np.zeros((1, 1, 1))
    # Populate the first character of target sequence with the start character.
    target_seq[0, 0, :] = 0

    # Sampling loop for a batch of sequences
    # (to simplify, here we assume a batch of size 1).
    decoded_sentence = ''
    foo=0
    prob = 0
    while foo < input_seq.shape[1]:
        #target_seq[0, 0, 0] = input_seq[0, foo, 0]
        output_tokens, h, c, lh, lc = decoder_model.predict(
            [target_seq, encoder_outputs] + states_value)

        # Sample a token
        sampled_token_index = np.argmax(output_tokens[0, -1, :])
        sampled_char = encoding.char_list[sampled_token_index]
        decoded_sentence += sampled_char
        prob += math.log(output_tokens[0, -1, sampled_token_index])

        # Update the target sequence (of length 1).
        target_seq = np.zeros((1, 1, 1))
        target_seq[0, 0, 0] = sampled_token_index

        # Update states
        states_value = [h, c, lh, lc]
        foo = foo+1
    print(prob)
    return decoded_sentence

def beam_decode_sequence(models, input_seq):
    [encoder_model, decoder_model] = models
    # Encode the input as state vectors.
    B = 10
    encoder_outputs, state_h, state_c = encoder_model.predict(input_seq[:,:,0:1])
    lstate_h = lstate_c = np.zeros((1, latent_dim))
    in_nbest=[(0., '', np.array([[[0]]]), [state_h, state_c, lstate_h, lstate_c])]
    foo=0
    while foo < input_seq.shape[1]:
        out_nbest = []
        for prob, decoded_sentence, target_seq, states_value in in_nbest:
            output_tokens, h, c, lh, lc = decoder_model.predict(
                [target_seq, encoder_outputs] + states_value)
            arg = np.argsort(output_tokens[0, -1, :])
            # Sample a token
            # Update states
            states_value = [h, c, lh, lc]
            for i in range(B):
                sampled_token_index = arg[-1-i]
                sampled_char = encoding.char_list[sampled_token_index]
                # Update the target sequence (of length 1).
                target_seq = np.array([[[sampled_token_index]]])
                new_prob = prob + math.log(output_tokens[0, -1, sampled_token_index])
                candidate = (new_prob, decoded_sentence + sampled_char, target_seq, states_value)
                if len(out_nbest) < B:
                    out_nbest.append(candidate)
                elif new_prob > out_nbest[-1][0]:
                    for j in range(len(out_nbest)):
                        if new_prob > out_nbest[j][0]:
                            out_nbest = out_nbest[:j] + [candidate] + out_nbest[j+1:]
                            break
        
        in_nbest = out_nbest
        foo = foo+1
    print(in_nbest[0][0])
    return in_nbest[0][1]



def decode_ground_truth(models, input_seq, output_seq):
    [encoder_model, decoder_model] = models
    # Encode the input as state vectors.
    encoder_outputs, state_h, state_c = encoder_model.predict(input_seq[:,:,0:1])
    lstate_h = lstate_c = np.zeros((1, latent_dim))
    states_value = [state_h, state_c, lstate_h, lstate_c]

    # Generate empty target sequence of length 1.
    target_seq = np.zeros((1, 1, 1))
    # Populate the first character of target sequence with the start character.
    target_seq[0, 0, :] = 0

    # Sampling loop for a batch of sequences
    # (to simplify, here we assume a batch of size 1).
    decoded_sentence = ''
    foo=0
    prob = 0
    while foo < input_seq.shape[1]:
        #target_seq[0, 0, 0] = input_seq[0, foo, 0]
        output_tokens, h, c, lh, lc = decoder_model.predict(
            [target_seq, encoder_outputs] + states_value)

        # Sample a token
        sampled_token_index = output_seq[foo]
        sampled_char = encoding.char_list[sampled_token_index]
        decoded_sentence += sampled_char
        prob += math.log(output_tokens[0, -1, sampled_token_index])

        # Update the target sequence (of length 1).
        target_seq = np.zeros((1, 1, 1))
        target_seq[0, 0, 0] = sampled_token_index

        # Update states
        states_value = [h, c, lh, lc]
        foo = foo+1
    print(prob)
    return prob
