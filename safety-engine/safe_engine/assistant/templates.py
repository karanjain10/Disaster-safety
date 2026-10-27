"""Pre-translated templates for safety-critical statements.

Safety-critical text shown to users is ALWAYS produced from these templates and
values verified by the gate — never from free LLM output. Hindi and Tamil
strings should be reviewed by native speakers before deployment.
"""
from __future__ import annotations

T = {
    "route_ok": {
        "en": "Verified route {route_id}: {distance_km} km, about {duration_min} min. Safety probability {p_safe}%. {margin_text}",
        "hi": "सत्यापित मार्ग {route_id}: {distance_km} किमी, लगभग {duration_min} मिनट। सुरक्षा संभावना {p_safe}%। {margin_text}",
        "ta": "சரிபார்க்கப்பட்ட வழி {route_id}: {distance_km} கி.மீ., சுமார் {duration_min} நிமிடம். பாதுகாப்பு நிகழ்தகவு {p_safe}%. {margin_text}",
    },
    "margin": {
        "en": "Predicted safety margin: {margin_min} min before the first hazard on this route.",
        "hi": "अनुमानित सुरक्षा अंतराल: इस मार्ग पर पहले खतरे से {margin_min} मिनट पहले।",
        "ta": "கணிக்கப்பட்ட பாதுகாப்பு இடைவெளி: இந்த வழியில் முதல் ஆபத்துக்கு {margin_min} நிமிடம் முன்.",
    },
    "margin_none": {
        "en": "No hazard is predicted on this route within the forecast window.",
        "hi": "पूर्वानुमान अवधि में इस मार्ग पर कोई खतरा अनुमानित नहीं है।",
        "ta": "முன்னறிவிப்பு காலத்தில் இந்த வழியில் எந்த ஆபத்தும் கணிக்கப்படவில்லை.",
    },
    "no_route": {
        "en": "No route can be verified as safe for a {vehicle} right now. Do not drive through flooded roads. If it is safe, move to the nearest higher ground or shelter, and call 112 in an emergency.",
        "hi": "अभी {vehicle} के लिए कोई भी मार्ग सुरक्षित सत्यापित नहीं किया जा सकता। बाढ़ वाली सड़कों से वाहन न चलाएँ। यदि सुरक्षित हो तो निकटतम ऊँचे स्थान या आश्रय पर जाएँ, और आपात स्थिति में 112 पर कॉल करें।",
        "ta": "தற்போது {vehicle} க்கு எந்த வழியும் பாதுகாப்பானது என உறுதிப்படுத்த முடியவில்லை. வெள்ளம் உள்ள சாலைகளில் வாகனம் ஓட்ட வேண்டாம். பாதுகாப்பாக இருந்தால் அருகிலுள்ள உயரமான இடம் அல்லது தங்குமிடத்திற்குச் செல்லுங்கள்; அவசரநிலையில் 112 ஐ அழைக்கவும்.",
    },
    "shelter": {
        "en": "Nearest reachable shelter: {name} ({shelter_id}), {distance_km} km, about {duration_min} min by verified route {route_id}.",
        "hi": "निकटतम पहुँच योग्य आश्रय: {name} ({shelter_id}), {distance_km} किमी, सत्यापित मार्ग {route_id} से लगभग {duration_min} मिनट।",
        "ta": "அருகிலுள்ள அணுகக்கூடிய தங்குமிடம்: {name} ({shelter_id}), {distance_km} கி.மீ., சரிபார்க்கப்பட்ட வழி {route_id} மூலம் சுமார் {duration_min} நிமிடம்.",
    },
    "hazard": {
        "en": "Warning ({hazard_id}): {hazard} predicted {where} from {start} to {end}, probability {p}%{intensity_text}.",
        "hi": "चेतावनी ({hazard_id}): {where} {start} से {end} तक {hazard} का अनुमान, संभावना {p}%{intensity_text}।",
        "ta": "எச்சரிக்கை ({hazard_id}): {where} {start} முதல் {end} வரை {hazard} கணிக்கப்பட்டுள்ளது, நிகழ்தகவு {p}%{intensity_text}.",
    },
    "report_ack": {
        "en": "Thank you. Your report {evidence_id} was recorded and will be checked before it affects routes.",
        "hi": "धन्यवाद। आपकी रिपोर्ट {evidence_id} दर्ज कर ली गई है और मार्गों को प्रभावित करने से पहले इसकी जाँच की जाएगी।",
        "ta": "நன்றி. உங்கள் அறிக்கை {evidence_id} பதிவு செய்யப்பட்டது; வழிகளைப் பாதிக்கும் முன் சரிபார்க்கப்படும்.",
    },
    "stale": {
        "en": "Hazard data is {age_min} minutes old and may be out of date, so no route or shelter can be confirmed. Follow official instructions and call 112 in an emergency.",
        "hi": "खतरे का डेटा {age_min} मिनट पुराना है और अद्यतन नहीं हो सकता, इसलिए किसी मार्ग या आश्रय की पुष्टि नहीं की जा सकती। आधिकारिक निर्देशों का पालन करें और आपात स्थिति में 112 पर कॉल करें।",
        "ta": "ஆபத்துத் தரவு {age_min} நிமிடம் பழையது, புதுப்பிக்கப்படாமல் இருக்கலாம்; எனவே எந்த வழியையும் தங்குமிடத்தையும் உறுதிப்படுத்த முடியாது. அதிகாரப்பூர்வ வழிகாட்டுதல்களைப் பின்பற்றி, அவசரநிலையில் 112 ஐ அழைக்கவும்.",
    },
    "refuse": {
        "en": "I could not verify an answer against current hazard data. Please follow official instructions; in an emergency call 112.",
        "hi": "मैं वर्तमान खतरा डेटा से किसी उत्तर का सत्यापन नहीं कर सका। कृपया आधिकारिक निर्देशों का पालन करें; आपात स्थिति में 112 पर कॉल करें।",
        "ta": "தற்போதைய ஆபத்துத் தரவுடன் பதிலை உறுதிப்படுத்த முடியவில்லை. அதிகாரப்பூர்வ வழிகாட்டுதல்களைப் பின்பற்றுங்கள்; அவசரநிலையில் 112 ஐ அழைக்கவும்.",
    },
    "no_hazard": {
        "en": "No active hazard is currently predicted within {radius_km} km of your location.",
        "hi": "आपके स्थान के {radius_km} किमी के भीतर वर्तमान में कोई सक्रिय खतरा अनुमानित नहीं है।",
        "ta": "உங்கள் இடத்திலிருந்து {radius_km} கி.மீ. சுற்றளவில் தற்போது எந்த ஆபத்தும் கணிக்கப்படவில்லை.",
    },
    # ---- M8: point-of-no-return ("leave-by") statements ---------------------------------
    "ponr_countdown": {
        "en": "Leave within {minutes} min (by {leave_by}) to keep this route safe for your {vehicle}. After that it is predicted to be cut off by {hazard}.",
        "hi": "इस मार्ग को अपने {vehicle} के लिए सुरक्षित रखने के लिए {minutes} मिनट के भीतर ({leave_by} तक) निकलें। उसके बाद {hazard} के कारण यह कट जाने का अनुमान है।",
        "ta": "இந்த வழி உங்கள் {vehicle} க்குப் பாதுகாப்பாக இருக்க {minutes} நிமிடத்திற்குள் ({leave_by} க்குள்) புறப்படுங்கள். அதன் பிறகு {hazard} காரணமாக இந்த வழி துண்டிக்கப்படும் என கணிக்கப்படுகிறது.",
    },
    "ponr_now": {
        "en": "Leave now. This route is predicted to be cut off by {hazard} in about {minutes} min.",
        "hi": "अभी निकलें। {hazard} के कारण यह मार्ग लगभग {minutes} मिनट में कट जाने का अनुमान है।",
        "ta": "இப்போதே புறப்படுங்கள். {hazard} காரணமாக இந்த வழி சுமார் {minutes} நிமிடத்தில் துண்டிக்கப்படும் என கணிக்கப்படுகிறது.",
    },
    "ponr_clear": {
        "en": "No cut-off is predicted on this route for the next {minutes} min. This is rechecked whenever the forecast updates.",
        "hi": "अगले {minutes} मिनट में इस मार्ग पर कोई अवरोध अनुमानित नहीं है। पूर्वानुमान बदलने पर इसकी फिर जाँच होती है।",
        "ta": "அடுத்த {minutes} நிமிடங்களுக்கு இந்த வழியில் துண்டிப்பு எதுவும் கணிக்கப்படவில்லை. முன்னறிவிப்பு மாறும்போது இது மீண்டும் சரிபார்க்கப்படும்.",
    },
    "ponr_blocked": {
        "en": "This route is not safe for your {vehicle} right now. Do not drive through water. If it is safe, move to higher ground or the nearest shelter; call 112 in an emergency.",
        "hi": "यह मार्ग अभी आपके {vehicle} के लिए सुरक्षित नहीं है। पानी से वाहन न चलाएँ। यदि सुरक्षित हो तो ऊँचे स्थान या निकटतम आश्रय पर जाएँ; आपात स्थिति में 112 पर कॉल करें।",
        "ta": "இந்த வழி இப்போது உங்கள் {vehicle} க்குப் பாதுகாப்பானது அல்ல. தண்ணீரில் வாகனம் ஓட்ட வேண்டாம். பாதுகாப்பாக இருந்தால் உயரமான இடம் அல்லது அருகிலுள்ள தங்குமிடத்திற்குச் செல்லுங்கள்; அவசரநிலையில் 112 ஐ அழைக்கவும்.",
    },
    "ponr_reopens": {
        "en": "It may become passable again in about {minutes} min.",
        "hi": "लगभग {minutes} मिनट में यह फिर से चलने योग्य हो सकता है।",
        "ta": "சுமார் {minutes} நிமிடத்தில் இது மீண்டும் செல்லக்கூடியதாக இருக்கலாம்.",
    },
    "ponr_trip": {
        "en": "On this trip you can afford at most {minutes} min of delay. After that the road ahead is predicted to be cut off by {hazard}.",
        "hi": "इस यात्रा में आप अधिकतम {minutes} मिनट की देरी सह सकते हैं। उसके बाद {hazard} के कारण आगे की सड़क कट जाने का अनुमान है।",
        "ta": "இந்தப் பயணத்தில் அதிகபட்சம் {minutes} நிமிட தாமதம் மட்டுமே தாங்க முடியும். அதன் பிறகு {hazard} காரணமாக முன்னால் உள்ள சாலை துண்டிக்கப்படும் என கணிக்கப்படுகிறது.",
    },
    "ponr_trip_urgent": {
        "en": "Little time left: the road ahead is predicted to be cut off by {hazard} in about {minutes} min.",
        "hi": "समय बहुत कम है: {hazard} के कारण आगे की सड़क लगभग {minutes} मिनट में कट जाने का अनुमान है।",
        "ta": "நேரம் குறைவு: {hazard} காரணமாக முன்னால் உள்ள சாலை சுமார் {minutes} நிமிடத்தில் துண்டிக்கப்படும் என கணிக்கப்படுகிறது.",
    },
}

HAZARD_NAMES = {
    "flood": {"en": "flooding", "hi": "बाढ़", "ta": "வெள்ளம்"},
    "landslide": {"en": "landslide", "hi": "भूस्खलन", "ta": "நிலச்சரிவு"},
    "storm": {"en": "strong wind / debris", "hi": "तेज़ हवा / मलबा", "ta": "பலத்த காற்று / இடிபாடுகள்"},
    "closure": {"en": "road closure", "hi": "सड़क बंद", "ta": "சாலை மூடல்"},
    "risk": {"en": "rising hazard risk", "hi": "बढ़ता खतरा", "ta": "அதிகரிக்கும் ஆபத்து"},
}
VEHICLE_NAMES = {
    "two_wheeler": {"en": "two-wheeler", "hi": "दोपहिया वाहन", "ta": "இருசக்கர வாகனம்"},
    "car": {"en": "car", "hi": "कार", "ta": "கார்"},
    "suv": {"en": "SUV", "hi": "एसयूवी", "ta": "எஸ்யூவி"},
    "ambulance": {"en": "ambulance", "hi": "एम्बुलेंस", "ta": "ஆம்புலன்ஸ்"},
    "bus": {"en": "bus", "hi": "बस", "ta": "பேருந்து"},
    "truck": {"en": "truck", "hi": "ट्रक", "ta": "லாரி"},
}
WHERE = {
    "here": {"en": "at your location", "hi": "आपके स्थान पर", "ta": "உங்கள் இடத்தில்"},
    "near": {"en": "{d} km from you", "hi": "आपसे {d} किमी दूर", "ta": "உங்களிடமிருந்து {d} கி.மீ. தொலைவில்"},
}


def render(key: str, lang: str, **kw) -> str:
    tpl = T[key].get(lang) or T[key]["en"]
    return tpl.format(**kw)


def name(table: dict, key: str, lang: str) -> str:
    return table.get(key, {}).get(lang) or table.get(key, {}).get("en") or key
