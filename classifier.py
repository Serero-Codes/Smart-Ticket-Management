import os
import joblib

_model = None

def _get_model():
    global _model
    if _model is None:
        model_path = os.path.join(os.path.dirname(__file__), "models", "ticket_classifier.pkl")
        _model = joblib.load(model_path)
    return _model


def classify_ticket(ticket_text: str) -> tuple[str, float]:
    model = _get_model()
    prediction = model.predict([ticket_text])[0]
    probability = model.predict_proba([ticket_text]).max()
    return prediction, round(float(probability) * 100, 2)
