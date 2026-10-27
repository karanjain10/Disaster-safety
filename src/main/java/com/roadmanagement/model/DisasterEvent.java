package com.roadmanagement.model;

public class DisasterEvent {

    private String city;
    private String weather;
    private int temperature;
    private int rainfall;
    private int year;
    private String disasterType;
    private int humidity;
    private int windSpeed;
    private String roadPassability;
    private String riskLevel;

    public String getCity() { return city; }
    public void setCity(String city) { this.city = city; }

    public String getWeather() { return weather; }
    public void setWeather(String weather) { this.weather = weather; }

    public int getTemperature() { return temperature; }
    public void setTemperature(int temperature) { this.temperature = temperature; }

    public int getRainfall() { return rainfall; }
    public void setRainfall(int rainfall) { this.rainfall = rainfall; }

    public int getYear() { return year; }
    public void setYear(int year) { this.year = year; }

    public String getDisasterType() { return disasterType; }
    public void setDisasterType(String disasterType) { this.disasterType = disasterType; }

    public int getHumidity() { return humidity; }
    public void setHumidity(int humidity) { this.humidity = humidity; }

    public int getWindSpeed() { return windSpeed; }
    public void setWindSpeed(int windSpeed) { this.windSpeed = windSpeed; }

    public String getRoadPassability() { return roadPassability; }
    public void setRoadPassability(String roadPassability) { this.roadPassability = roadPassability; }

    public String getRiskLevel() { return riskLevel; }
    public void setRiskLevel(String riskLevel) { this.riskLevel = riskLevel; }
}