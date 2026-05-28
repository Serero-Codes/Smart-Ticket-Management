import pandas as pd
import joblib

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline

# Load training data

data = pd.read_csv("training_data/tickets.csv")

# Features and labels
X = data["text"]
y = data["category"]

# Create AI pipeline
model = Pipeline([
    ('tfidf', TfidfVectorizer()),
    ('classifier', MultinomialNB())
])

# Train model
model.fit(X, y)

# Save model
joblib.dump(model, 'models/ticket_classifier.pkl')

print("Model trained successfully!")