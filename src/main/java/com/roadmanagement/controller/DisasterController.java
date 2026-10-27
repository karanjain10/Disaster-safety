package com.roadmanagement.controller;

import com.roadmanagement.service.DisasterPredictionService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/disaster")
public class DisasterController {

    @Autowired
    private DisasterPredictionService prediction;

    @GetMapping("/predict")
    public String predict(
            @RequestParam String city,
            @RequestParam String weather,
            @RequestParam int temperature,
            @RequestParam int rainfall,
            @RequestParam(required = false, defaultValue = "0") int humidity,
            @RequestParam(required = false, defaultValue = "0") int windSpeed
    ) throws Exception {

        return prediction.predict(city, weather, temperature, rainfall, humidity, windSpeed);
    }
}