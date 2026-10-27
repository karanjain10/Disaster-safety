package com.roadmanagement.util;

import com.roadmanagement.model.DisasterEvent;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

public class DatasetLoader {

    public static List<DisasterEvent> load() {

        List<DisasterEvent> events = new ArrayList<>();

        try {

            InputStream is = DatasetLoader.class
                    .getClassLoader()
                    .getResourceAsStream("data/india_disaster_weather_dataset_500_rows.csv");

            if (is == null)
                throw new RuntimeException("Dataset not found");

            BufferedReader br = new BufferedReader(new InputStreamReader(is));

            String line;

            br.readLine(); // skip header

            while ((line = br.readLine()) != null) {

                String[] parts = line.split(",");

                if (parts.length < 6)
                    continue;

                DisasterEvent e = new DisasterEvent();

                e.setCity(parts[0].trim());
                e.setDisasterType(parts[1].trim());
                e.setWeather(parts[2].trim());
                e.setYear(Integer.parseInt(parts[3].trim()));
                e.setTemperature(Integer.parseInt(parts[4].trim()));
                e.setRainfall(Integer.parseInt(parts[5].trim()));

                if (parts.length >= 8) {
                    try {
                        e.setHumidity(Integer.parseInt(parts[6].trim()));
                        e.setWindSpeed(Integer.parseInt(parts[7].trim()));
                    } catch (NumberFormatException ignored) {}
                }
                if (parts.length >= 10) {
                    e.setRoadPassability(parts[8].trim());
                    e.setRiskLevel(parts[9].trim());
                }

                events.add(e);
            }

            br.close();

        } catch (Exception e) {
            throw new RuntimeException("Dataset loading failed", e);
        }

        return events;
    }
}