package com.roadmanagement.service;

import com.roadmanagement.model.DisasterEvent;
import com.roadmanagement.util.DatasetLoader;
import weka.classifiers.trees.RandomForest;
import weka.core.*;

import java.util.ArrayList;
import java.util.List;

public class RandomForestTrainingService {

    private RandomForest model;
    private Instances structure;

    public RandomForestTrainingService() {

        try {

            List<DisasterEvent> events = DatasetLoader.load();

            ArrayList<Attribute> attributes = new ArrayList<>();

            attributes.add(new Attribute("temperature"));
            attributes.add(new Attribute("rainfall"));
            attributes.add(new Attribute("humidity"));
            attributes.add(new Attribute("windSpeed"));
            attributes.add(new Attribute("year"));

            /* CITY */

            ArrayList<String> cities = new ArrayList<>();

            for(DisasterEvent e : events)
                if(!cities.contains(e.getCity()))
                    cities.add(e.getCity());

            Attribute cityAttr = new Attribute("city", cities);
            attributes.add(cityAttr);

            /* WEATHER */

            ArrayList<String> weatherValues = new ArrayList<>();

            for(DisasterEvent e : events)
                if(!weatherValues.contains(e.getWeather()))
                    weatherValues.add(e.getWeather());

            Attribute weatherAttr = new Attribute("weather", weatherValues);
            attributes.add(weatherAttr);

            /* DISASTER TYPE */

            ArrayList<String> disasterValues = new ArrayList<>();

            for(DisasterEvent e : events)
                if(!disasterValues.contains(e.getDisasterType()))
                    disasterValues.add(e.getDisasterType());

            Attribute classAttr = new Attribute("disasterType", disasterValues);
            attributes.add(classAttr);

            Instances dataset = new Instances("DisasterDataset", attributes, events.size());

            dataset.setClassIndex(attributes.size() - 1);

            for(DisasterEvent e : events){

                DenseInstance inst = new DenseInstance(attributes.size());

                inst.setDataset(dataset);

                inst.setValue(dataset.attribute("temperature"), e.getTemperature());
                inst.setValue(dataset.attribute("rainfall"), e.getRainfall());
                inst.setValue(dataset.attribute("humidity"), e.getHumidity() > 0 ? e.getHumidity() : (e.getRainfall() > 50 ? 85 : 55));
                inst.setValue(dataset.attribute("windSpeed"), e.getWindSpeed() > 0 ? e.getWindSpeed() : 25);
                inst.setValue(dataset.attribute("year"), e.getYear());
                inst.setValue(dataset.attribute("city"), e.getCity());
                inst.setValue(dataset.attribute("weather"), e.getWeather());
                inst.setValue(dataset.attribute("disasterType"), e.getDisasterType());

                dataset.add(inst);
            }

            model = new RandomForest();

            model.setNumIterations(100);

            model.buildClassifier(dataset);

            structure = dataset;

            System.out.println("Model trained with rows: "+dataset.numInstances());

        }
        catch(Exception e){
            throw new RuntimeException(e);
        }
    }

    public String predict(String city,String weather,int temp,int rainfall,int year) throws Exception {
        int hum = rainfall > 100 ? 90 : (rainfall > 40 ? 78 : (temp > 40 ? 30 : 60));
        int wind = (weather.contains("Storm") || weather.contains("Gale") || weather.contains("Cyclone")) ? 85 : (rainfall > 80 ? 35 : 18);
        return predict(city, weather, temp, rainfall, hum, wind, year);
    }

    public String predict(String city, String weather, int temp, int rainfall, int humidity, int windSpeed, int year) throws Exception {
        DenseInstance inst = createInstance(city, weather, temp, rainfall, humidity, windSpeed, year);
        double result = model.classifyInstance(inst);
        return structure.classAttribute().value((int) result);
    }

    public double[] predictDistribution(String city, String weather, int temp, int rainfall, int humidity, int windSpeed, int year) throws Exception {
        DenseInstance inst = createInstance(city, weather, temp, rainfall, humidity, windSpeed, year);
        return model.distributionForInstance(inst);
    }

    private DenseInstance createInstance(String city, String weather, int temp, int rainfall, int humidity, int windSpeed, int year) {
        DenseInstance inst = new DenseInstance(structure.numAttributes());
        inst.setDataset(structure);

        inst.setValue(structure.attribute("temperature"), temp);
        inst.setValue(structure.attribute("rainfall"), rainfall);
        inst.setValue(structure.attribute("humidity"), humidity > 0 ? humidity : (rainfall > 50 ? 85 : 55));
        inst.setValue(structure.attribute("windSpeed"), windSpeed > 0 ? windSpeed : (weather.contains("Cyclone") ? 90 : 25));
        inst.setValue(structure.attribute("year"), year);

        Attribute cityAttr = structure.attribute("city");
        if (cityAttr.indexOfValue(city) >= 0) {
            inst.setValue(cityAttr, city);
        } else {
            inst.setValue(cityAttr, cityAttr.value(0));
        }

        Attribute weatherAttr = structure.attribute("weather");
        if (weatherAttr.indexOfValue(weather) >= 0) {
            inst.setValue(weatherAttr, weather);
        } else {
            inst.setValue(weatherAttr, weatherAttr.value(0));
        }

        return inst;
    }
}