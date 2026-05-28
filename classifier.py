import joblib

model = joblib.load('models/ticket_classifier.pkl')


def classify_ticket(ticket_text):
    prediction = model.predict([ticket_text])[0]
    probability = model.predict_proba([ticket_text]).max()

    return prediction, round(probability * 100, 2)