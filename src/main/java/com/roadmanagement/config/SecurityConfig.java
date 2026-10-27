package com.roadmanagement.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
public class SecurityConfig {

    /** Set app.security.enabled=false to run locally without Google OAuth credentials. */
    @Value("${app.security.enabled:false}")
    private boolean securityEnabled;

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {

        http.csrf(csrf -> csrf.disable());

        if (!securityEnabled) {
            http.authorizeHttpRequests(auth -> auth.anyRequest().permitAll());
            return http.build();
        }

        http
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(
                                "/",           // root
                                "/login",      // login page
                                "/css/**",
                                "/bg-map.png", "/js/**", "/images/**"
                        ).permitAll()

                        // EVERYTHING ELSE (including /engine/**) REQUIRES LOGIN
                        .anyRequest().authenticated()
                )

                .oauth2Login(oauth -> oauth
                        .loginPage("/login")
                        .defaultSuccessUrl("/", true)
                )

                .logout(logout -> logout
                        .logoutSuccessUrl("/login")
                        .permitAll()
                );

        return http.build();
    }
}
