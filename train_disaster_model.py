"""Train and evaluate the Road Hazard & Disaster Prediction Random Forest model.

Usage:
    python train_disaster_model.py

Dataset:
    India Weather-Induced Road Disaster Dataset (4,500 verified instances)
    Features: City, WeatherCondition, TemperatureC, RainfallMM, Humidity, WindSpeedKMH
    Target: DisasterType (Flood, Landslide, Cyclone, Waterlogging, Heatwave, None)
"""

import os
import json
import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score

def main():
    print("=" * 60)
    print(" ROAD DISASTER PREDICTION MODEL TRAINING (Review Benchmark)")
    print("=" * 60)

    # 1. Load Dataset
    data_path = os.path.join(os.path.dirname(__file__), "src", "main", "resources", "data", "india_road_disaster_weather_dataset.csv")
    if not os.path.exists(data_path):
        data_path = os.path.join(os.path.dirname(__file__), "src", "main", "resources", "data", "india_disaster_weather_dataset_500_rows.csv")

    df = pd.read_csv(data_path)
    print(f"Loaded dataset: {data_path}")
    print(f"Total Records : {len(df)}")
    print(f"Columns       : {list(df.columns)}")
    print("\nClass Distribution:")
    for cls_name, count in df['DisasterType'].value_counts().items():
        print(f"  - {cls_name:<15}: {count} records ({count/len(df)*100:.1f}%)")

    # 2. Preprocess & Feature Engineering
    feature_cols = ['TemperatureC', 'RainfallMM', 'Humidity', 'WindSpeedKMH', 'City', 'WeatherCondition']
    X_raw = df[feature_cols]
    y = df['DisasterType']

    X = pd.get_dummies(X_raw, drop_first=False)
    feature_names = list(X.columns)

    # 3. Train-Test Split (80% train, 20% test)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, random_state=42, stratify=y)
    print(f"\nTrain size: {len(X_train)} rows | Test size: {len(X_test)} rows")

    # 4. Train Random Forest Classifier
    rf = RandomForestClassifier(n_estimators=100, max_depth=16, min_samples_leaf=1, random_state=42, n_jobs=-1)
    rf.fit(X_train, y_train)

    # 5. Evaluate
    y_pred = rf.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    report_dict = classification_report(y_test, y_pred, output_dict=True)
    report_text = classification_report(y_test, y_pred)

    print("\n" + "=" * 60)
    print(f" TEST ACCURACY: {acc * 100:.2f}%")
    print("=" * 60)
    print("\nClassification Report:\n")
    print(report_text)

    # 6. Top Feature Importances
    importances = rf.feature_importances_
    sorted_idx = np.argsort(importances)[::-1]
    print("\nTop 8 Most Predictive Features:")
    for rank, idx in enumerate(sorted_idx[:8], 1):
        print(f"  {rank}. {feature_names[idx]:<25}: {importances[idx]*100:.2f}% importance")

    # 7. Save Model & Metrics
    models_dir = os.path.join(os.path.dirname(__file__), "safety-engine", "models")
    os.makedirs(models_dir, exist_ok=True)
    model_save_path = os.path.join(models_dir, "road_disaster_model.pkl")
    meta_save_path = os.path.join(models_dir, "road_disaster_model_metrics.json")

    joblib.dump({"model": rf, "feature_names": feature_names, "classes": list(rf.classes_)}, model_save_path)
    print(f"\nModel saved to: {model_save_path}")

    metrics = {
        "dataset_name": "India Weather-Induced Road Disaster Dataset",
        "total_rows": len(df),
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "accuracy": float(acc),
        "classes": list(rf.classes_),
        "classification_report": report_dict,
        "top_features": [{feature_names[i]: float(importances[i])} for i in sorted_idx[:10]]
    }

    with open(meta_save_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to: {meta_save_path}")
    print("\nModel training & review artifacts generated successfully!")

if __name__ == "__main__":
    main()
