"""
The MARS CNN remade in Keras 3: 
    3x [Conv2D 32/64/128 + MaxPool] -> Dense 256/512 -> Dense(2, sigmoid)
    binary_crossentropy on one-hot labels 
    The same layers, loss, epochs, and batch size as the original cnn-model.py.


"""
import keras
from keras import layers, ops, backend

from offdevice.cnn_quant.features import params


# ---- architecture -----------------------------------------------------------
def build_model(input_shape=(params.N_BINS, params.N_FEATURES, 1)):
    model = keras.Sequential()
    model.add(layers.Input(shape=input_shape))
    model.add(layers.Conv2D(32, 3, padding="same", activation="relu"))
    model.add(layers.MaxPooling2D(padding="same"))
    model.add(layers.Conv2D(64, 3, padding="same", activation="relu"))
    model.add(layers.MaxPooling2D(padding="same"))
    model.add(layers.Conv2D(128, 3, padding="same", activation="relu"))
    model.add(layers.MaxPooling2D(padding="same"))
    model.add(layers.Dropout(0.3))
    model.add(layers.Flatten())
    model.add(layers.Dense(256, activation="relu"))
    model.add(layers.Dropout(0.3))
    model.add(layers.Dense(512, activation="relu"))
    model.add(layers.Dropout(0.3))
    model.add(layers.Dense(2, activation="sigmoid"))
    return model

# ---- train / eval -----------------------------------------------------------
def train_model(model, x_train, y_train, x_test, y_test, save_path=f"mars_cnn_{params.ACTIVE_MODE.lower()}.keras"):
    model.compile(
        optimizer="adam",
        loss="binary_crossentropy",
        metrics=["accuracy", f1_m, precision_m, recall_m],
    )
    model.summary()
    history = model.fit(
        x_train, y_train,
        batch_size=6,
        epochs=250,
        validation_data=(x_test, y_test),
        verbose=1,
    )
    model.save(save_path)
    return model, history



# ---- metrics -----------------------------------------------------------
# Ported from the original MARS (Keras 2 K.* idiom).
def recall_m(y_true, y_pred):
    true_positives = ops.sum(ops.round(ops.clip(y_true * y_pred, 0, 1)))
    possible_positives = ops.sum(ops.round(ops.clip(y_true, 0, 1)))
    recall = true_positives / (possible_positives + backend.epsilon())
    return recall

def precision_m(y_true, y_pred):
    true_positives = ops.sum(ops.round(ops.clip(y_true * y_pred, 0, 1)))
    predicted_positives = ops.sum(ops.round(ops.clip(y_pred, 0, 1)))
    precision = true_positives / (predicted_positives + backend.epsilon())
    return precision

def f1_m(y_true, y_pred):
    precision = precision_m(y_true, y_pred)
    recall = recall_m(y_true, y_pred)
    return 2*((precision * recall)/(precision + recall + backend.epsilon())) 
    

def evaluate_model(model, x_test, y_test):
    results = model.evaluate(x_test, y_test, verbose=0)
    loss, accuracy, f1_score, precision, recall = results
    print(f"Loss      : {loss}")
    print(f"Accuracy  : {accuracy}")
    print(f"F1 Score  : {f1_score}")
    print(f"Precision : {precision}")
    print(f"Recall    : {recall}")

