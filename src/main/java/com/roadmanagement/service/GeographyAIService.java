package com.roadmanagement.service;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.client.RestTemplate;
import org.springframework.http.*;

import java.util.Map;
import java.util.List;
import java.util.HashMap;
import java.util.ArrayList;

@Service
public class GeographyAIService {

    @Value("${groq.api.key:}")
    private String groqApiKey;

    @Value("${groq.model:llama-3.3-70b-versatile}")
    private String model;

    @Value("${groq.temperature:0.2}")
    private double temperature;

    private final RestTemplate restTemplate = new RestTemplate();

    public String getAnswer(String question) {

        String prompt =
                "You are an AI assistant for a Road Management System. " +
                        "Answer strictly from a geographical and infrastructure perspective. " +
                        "Focus on terrain, road connectivity, disaster risk, and traffic planning.\n\n" +
                        "Question: " + question;

        // Build request body
        Map<String, Object> requestBody = new HashMap<>();
        requestBody.put("model", model);
        requestBody.put("temperature", temperature);
        requestBody.put("stream", false);

        List<Map<String, String>> messages = new ArrayList<>();
        messages.add(Map.of("role", "user", "content", prompt));

        requestBody.put("messages", messages);

        try {

            // Headers
            HttpHeaders headers = new HttpHeaders();
            headers.setContentType(MediaType.APPLICATION_JSON);
            headers.setBearerAuth(groqApiKey);

            HttpEntity<Map<String, Object>> entity =
                    new HttpEntity<>(requestBody, headers);

            // API call
            ResponseEntity<Map> response = restTemplate.exchange(
                    "https://api.groq.com/openai/v1/chat/completions",
                    HttpMethod.POST,
                    entity,
                    Map.class
            );

            Map responseBody = response.getBody();

            if (responseBody == null || !responseBody.containsKey("choices")) {
                return "No valid response from AI.";
            }

            List choices = (List) responseBody.get("choices");
            Map firstChoice = (Map) choices.get(0);
            Map message = (Map) firstChoice.get("message");

            return message.get("content").toString();

        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }
}