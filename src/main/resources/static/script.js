console.log("JS Loaded");

var map = L.map('map').setView([28.7041, 77.1025], 12);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {})
    .addTo(map);

let routeLayers = [];
let disasterCircle = null;

// ================= ROUTING =================

function calculateRoute() {

    const startLat = parseFloat(document.getElementById("startLat").value);
    const startLon = parseFloat(document.getElementById("startLon").value);
    const endLat = parseFloat(document.getElementById("endLat").value);
    const endLon = parseFloat(document.getElementById("endLon").value);

    let disasterLat = parseFloat(document.getElementById("disLat").value);
    let disasterLon = parseFloat(document.getElementById("disLon").value);
    let disasterRadius = parseFloat(document.getElementById("disRadius").value);

    // ✅ FIX NaN issue
    if (isNaN(disasterLat)) disasterLat = null;
    if (isNaN(disasterLon)) disasterLon = null;
    if (isNaN(disasterRadius)) disasterRadius = null;

    fetch("/api/route", {
        method:"POST",
        headers:{ "Content-Type":"application/json" },
        body:JSON.stringify({
            startLat,
            startLon,
            endLat,
            endLon,
            disasterLat,
            disasterLon,
            disasterRadius
        })
    })
    .then(res=>res.json())
    .then(routes=>{

        // ✅ safety check
        if (!Array.isArray(routes)) {
            console.error("Invalid response:", routes);
            alert("Error fetching routes");
            return;
        }

        routeLayers.forEach(layer=>map.removeLayer(layer));
        routeLayers=[];

        if(routes.length===0){
            alert("No safe routes available!");
            return;
        }

        routes.forEach(route=>{

            const geoLayer=L.geoJSON({
                type:"Feature",
                geometry:route.geometry
            },{
                style:{color:"green",weight:6}
            }).addTo(map);

            routeLayers.push(geoLayer);

            document.getElementById("distance").innerText =
                "Distance: "+(route.distance/1000).toFixed(2)+" km";

            document.getElementById("duration").innerText =
                "ETA: "+(route.duration/60).toFixed(2)+" mins";
        });

        map.fitBounds(routeLayers[0].getBounds());
    });
}

// ================= GEO AI =================

function askGeoAI(){

    const question=document.getElementById("geoQuestion").value;

    fetch("/geo-ai/ask",{
        method:"POST",
        headers:{ "Content-Type":"application/json" },
        body:JSON.stringify({question})
    })
        .then(res=>res.text())
        .then(ans=>{
            document.getElementById("geoResult").innerText=ans;
        });
}


// ================= DISASTER =================

function setDisasterZone(){

    const lat=parseFloat(document.getElementById("disLat").value);
    const lon=parseFloat(document.getElementById("disLon").value);
    const radius=parseFloat(document.getElementById("disRadius").value);

    if(disasterCircle){
        map.removeLayer(disasterCircle);
    }

    disasterCircle=L.circle([lat,lon],{
        color:'red',
        fillColor:'red',
        fillOpacity:0.3,
        radius:radius
    }).addTo(map);

    map.setView([lat,lon],13);
}


// ================= PREDICTION =================

async function predictDisaster(){

    let city = document.getElementById("predCity").value || "Chennai";
    let weather = document.getElementById("predWeather").value;
    let temp = document.getElementById("predTemp").value || "28";
    let rain = document.getElementById("predRain").value || "100";
    let humidityElem = document.getElementById("predHumidity");
    let humidity = humidityElem ? (humidityElem.value || "0") : "0";
    let windElem = document.getElementById("predWind");
    let windSpeed = windElem ? (windElem.value || "0") : "0";

    let url = `/api/disaster/predict?city=${encodeURIComponent(city)}&weather=${encodeURIComponent(weather)}&temperature=${temp}&rainfall=${rain}&humidity=${humidity}&windSpeed=${windSpeed}`;

    document.getElementById("predictionResult").innerText = "Analyzing road hazards with Random Forest model...";

    try {
        let res = await fetch(url);
        let result = await res.text();
        document.getElementById("predictionResult").innerText = result;
    } catch (err) {
        document.getElementById("predictionResult").innerText = "Prediction error: " + err.message;
    }
}