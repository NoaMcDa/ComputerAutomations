"""Destination -> (country, continent) for the vacation-deal report.

The travel sites mostly give a Hebrew city name and only sometimes a country
(in Hebrew or English), so the lookup accepts all of these:

  * a country: English name, ISO-2 code or Hebrew name   ("Japan", "JP", "יפן")
  * a city / island / region, in Hebrew or English        ("בנגקוק", "Phuket")
  * a site title that contains one of the above           ("סיציליה (פלרמו)",
    "דורס | אלבניה", "טורונטו - ON", "מאורגן: 5 ימים בפראג")

`classify()` returns (ISO-2 country code, continent), or None when nothing
matches. Israel is classified as `DOMESTIC`: a domestic flight (e.g. Eilat)
is not a trip abroad and the report leaves it out.

Placement decisions: Georgia, Armenia, Azerbaijan, Cyprus, Turkey -> Europe;
UAE / the Gulf, Uzbekistan -> Asia; Egypt and Sinai -> Africa.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

EUROPE = "Europe"
ASIA = "Asia"
NORTH_AMERICA = "North America"
SOUTH_AMERICA = "South America"
AFRICA = "Africa"
OCEANIA = "Oceania"
DOMESTIC = "Domestic"   # Israel: classified, but not a destination abroad

CONTINENT_ORDER = (EUROPE, ASIA, NORTH_AMERICA, SOUTH_AMERICA, AFRICA, OCEANIA)
CONTINENT_HE = {
    EUROPE: "אירופה",
    ASIA: "אסיה",
    NORTH_AMERICA: "צפון אמריקה",
    SOUTH_AMERICA: "דרום אמריקה",
    AFRICA: "אפריקה",
    OCEANIA: "אוקיאניה",
}

# ISO-2 -> (English name, continent, Hebrew name, *other spellings)
_COUNTRIES: dict[str, tuple[str, ...]] = {
    # ---- Europe ----
    "AL": ("Albania", EUROPE, "אלבניה"),
    "AD": ("Andorra", EUROPE, "אנדורה"),
    "AM": ("Armenia", EUROPE, "ארמניה"),
    "AT": ("Austria", EUROPE, "אוסטריה"),
    "AZ": ("Azerbaijan", EUROPE, "אזרבייג'ן", "אזרבייגן", "אזרביג'ן", "אזרבידג'ן"),
    "BA": ("Bosnia and Herzegovina", EUROPE, "בוסניה", "בוסניה והרצגובינה", "Bosnia"),
    "BE": ("Belgium", EUROPE, "בלגיה"),
    "BG": ("Bulgaria", EUROPE, "בולגריה"),
    "BY": ("Belarus", EUROPE, "בלארוס", "בלרוס"),
    "CH": ("Switzerland", EUROPE, "שווייץ", "שוויץ"),
    "CY": ("Cyprus", EUROPE, "קפריסין"),
    "CZ": ("Czechia", EUROPE, "צ'כיה", "Czech Republic"),
    "DE": ("Germany", EUROPE, "גרמניה"),
    "DK": ("Denmark", EUROPE, "דנמרק"),
    "EE": ("Estonia", EUROPE, "אסטוניה"),
    "ES": ("Spain", EUROPE, "ספרד"),
    "FI": ("Finland", EUROPE, "פינלנד"),
    "FR": ("France", EUROPE, "צרפת"),
    "GB": ("United Kingdom", EUROPE, "בריטניה", "אנגליה", "סקוטלנד", "הממלכה המאוחדת",
           "UK", "England", "Great Britain", "Scotland"),
    "GE": ("Georgia", EUROPE, "גאורגיה", "גיאורגיה", "גרוזיה"),
    "GR": ("Greece", EUROPE, "יוון"),
    "HR": ("Croatia", EUROPE, "קרואטיה"),
    "HU": ("Hungary", EUROPE, "הונגריה"),
    "IE": ("Ireland", EUROPE, "אירלנד"),
    "IS": ("Iceland", EUROPE, "איסלנד"),
    "IT": ("Italy", EUROPE, "איטליה"),
    "LT": ("Lithuania", EUROPE, "ליטא"),
    "LU": ("Luxembourg", EUROPE, "לוקסמבורג"),
    "LV": ("Latvia", EUROPE, "לטביה"),
    "MC": ("Monaco", EUROPE, "מונקו"),
    "MD": ("Moldova", EUROPE, "מולדובה"),
    "ME": ("Montenegro", EUROPE, "מונטנגרו"),
    "MK": ("North Macedonia", EUROPE, "מקדוניה", "צפון מקדוניה", "Macedonia"),
    "MT": ("Malta", EUROPE, "מלטה"),
    "NL": ("Netherlands", EUROPE, "הולנד", "Holland", "The Netherlands"),
    "NO": ("Norway", EUROPE, "נורווגיה"),
    "PL": ("Poland", EUROPE, "פולין"),
    "PT": ("Portugal", EUROPE, "פורטוגל"),
    "RO": ("Romania", EUROPE, "רומניה"),
    "RS": ("Serbia", EUROPE, "סרביה"),
    "RU": ("Russia", EUROPE, "רוסיה"),
    "SE": ("Sweden", EUROPE, "שוודיה"),
    "SI": ("Slovenia", EUROPE, "סלובניה"),
    "SK": ("Slovakia", EUROPE, "סלובקיה"),
    "TR": ("Turkey", EUROPE, "טורקיה", "Türkiye", "Turkiye"),
    "UA": ("Ukraine", EUROPE, "אוקראינה"),
    # ---- Asia ----
    "AE": ("United Arab Emirates", ASIA, "איחוד האמירויות", "איחוד האמירויות הערביות",
           "האמירויות", "UAE", "Emirates"),
    "BH": ("Bahrain", ASIA, "בחריין"),
    "CN": ("China", ASIA, "סין"),
    "HK": ("Hong Kong", ASIA, "הונג קונג"),
    "ID": ("Indonesia", ASIA, "אינדונזיה"),
    "IN": ("India", ASIA, "הודו"),
    "JO": ("Jordan", ASIA, "ירדן"),
    "JP": ("Japan", ASIA, "יפן"),
    "KG": ("Kyrgyzstan", ASIA, "קירגיזסטן"),
    "KH": ("Cambodia", ASIA, "קמבודיה"),
    "KR": ("South Korea", ASIA, "דרום קוריאה", "קוריאה", "Korea"),
    "KZ": ("Kazakhstan", ASIA, "קזחסטן"),
    "LA": ("Laos", ASIA, "לאוס"),
    "LK": ("Sri Lanka", ASIA, "סרי לנקה"),
    "MN": ("Mongolia", ASIA, "מונגוליה"),
    "MO": ("Macau", ASIA, "מקאו"),
    "MV": ("Maldives", ASIA, "מלדיביים", "האיים המלדיביים", "איים המלדיביים", "מאלדיביים"),
    "MY": ("Malaysia", ASIA, "מלזיה"),
    "NP": ("Nepal", ASIA, "נפאל"),
    "OM": ("Oman", ASIA, "עומאן", "עומן"),
    "PH": ("Philippines", ASIA, "הפיליפינים", "פיליפינים"),
    "QA": ("Qatar", ASIA, "קטאר"),
    "SA": ("Saudi Arabia", ASIA, "ערב הסעודית", "סעודיה"),
    "SG": ("Singapore", ASIA, "סינגפור"),
    "TH": ("Thailand", ASIA, "תאילנד"),
    "TW": ("Taiwan", ASIA, "טייוואן", "טאיוואן"),
    "UZ": ("Uzbekistan", ASIA, "אוזבקיסטן"),
    "VN": ("Vietnam", ASIA, "וייטנאם", "ויאטנם", "Viet Nam"),
    # ---- North America (incl. Central America and the Caribbean) ----
    "BS": ("Bahamas", NORTH_AMERICA, "איי בהאמה", "בהאמה"),
    "CA": ("Canada", NORTH_AMERICA, "קנדה"),
    "CR": ("Costa Rica", NORTH_AMERICA, "קוסטה ריקה"),
    "CU": ("Cuba", NORTH_AMERICA, "קובה"),
    "DO": ("Dominican Republic", NORTH_AMERICA, "הרפובליקה הדומיניקנית", "דומיניקנה"),
    "GT": ("Guatemala", NORTH_AMERICA, "גואטמלה"),
    "JM": ("Jamaica", NORTH_AMERICA, "ג'מייקה"),
    "MX": ("Mexico", NORTH_AMERICA, "מקסיקו"),
    "PA": ("Panama", NORTH_AMERICA, "פנמה"),
    "US": ("United States", NORTH_AMERICA, 'ארה"ב', "ארצות הברית", "אמריקה",
           "USA", "U.S.A.", "United States of America"),
    # ---- South America ----
    "AR": ("Argentina", SOUTH_AMERICA, "ארגנטינה"),
    "BO": ("Bolivia", SOUTH_AMERICA, "בוליביה"),
    "BR": ("Brazil", SOUTH_AMERICA, "ברזיל"),
    "CL": ("Chile", SOUTH_AMERICA, "צ'ילה"),
    "CO": ("Colombia", SOUTH_AMERICA, "קולומביה"),
    "EC": ("Ecuador", SOUTH_AMERICA, "אקוודור"),
    "PE": ("Peru", SOUTH_AMERICA, "פרו"),
    "UY": ("Uruguay", SOUTH_AMERICA, "אורוגוואי"),
    # ---- Africa ----
    "EG": ("Egypt", AFRICA, "מצרים", "סיני", "Sinai"),
    "ET": ("Ethiopia", AFRICA, "אתיופיה"),
    "KE": ("Kenya", AFRICA, "קניה", "קנייה"),
    "MA": ("Morocco", AFRICA, "מרוקו"),
    "MG": ("Madagascar", AFRICA, "מדגסקר"),
    "MU": ("Mauritius", AFRICA, "מאוריציוס", "מאוריציוס"),
    "NA": ("Namibia", AFRICA, "נמיביה"),
    "RW": ("Rwanda", AFRICA, "רואנדה"),
    "SC": ("Seychelles", AFRICA, "סיישל", "איי סיישל", "Seychelles Islands"),
    "TN": ("Tunisia", AFRICA, "תוניסיה"),
    "TZ": ("Tanzania", AFRICA, "טנזניה"),
    "UG": ("Uganda", AFRICA, "אוגנדה"),
    "ZA": ("South Africa", AFRICA, "דרום אפריקה"),
    # ---- Oceania ----
    "AU": ("Australia", OCEANIA, "אוסטרליה"),
    "FJ": ("Fiji", OCEANIA, "פיג'י"),
    "NZ": ("New Zealand", OCEANIA, "ניו זילנד"),
    "PF": ("French Polynesia", OCEANIA, "פולינזיה הצרפתית", "טהיטי", "Tahiti"),
    # ---- Home ----
    "IL": ("Israel", DOMESTIC, "ישראל"),
}

# ISO-2 -> cities, islands and regions (Hebrew and English spellings).
_PLACES: dict[str, tuple[str, ...]] = {
    # ---- Europe ----
    "AL": ("טירנה", "Tirana", "דורס", "Durres", "סרנדה", "Saranda", "ולורה", "Vlore"),
    "AM": ("ירוואן", "ירבאן", "Yerevan"),
    "AT": ("וינה", "Vienna", "Wien", "זלצבורג", "Salzburg", "אינסברוק", "Innsbruck",
           "גראץ", "Graz", "צל אם זה", "Zell am See", "קיצביל", "Kitzbuhel", "איישגל", "Ischgl"),
    "AZ": ("באקו", "Baku", "גבלה", "קבלה", "Gabala", "Qabala", "שאחדאג", "Shahdag", "שקי", "Sheki"),
    "BA": ("סרייבו", "Sarajevo", "מוסטאר", "Mostar"),
    "BE": ("בריסל", "Brussels", "אנטוורפן", "Antwerp", "בריז'", "Bruges", "שרלרואה", "Charleroi"),
    "BG": ("סופיה", "Sofia", "בורגס", "Burgas", "ורנה", "Varna", "בנסקו", "Bansko",
           "בורובץ", "Borovets", "פמפורובו", "Pamporovo", "פלובדיב", "Plovdiv",
           "סאני ביץ'", "Sunny Beach", "חוף השמש", "גולדן סנדס", "Golden Sands"),
    "BY": ("מינסק", "Minsk"),
    "CH": ("ציריך", "Zurich", "ז'נבה", "Geneva", "בזל", "Basel", "ברן", "Bern", "לוצרן", "Lucerne",
           "אינטרלאקן", "Interlaken", "צרמט", "Zermatt", "לוגאנו", "Lugano"),
    "CY": ("לרנקה", "Larnaca", "פאפוס", "Paphos", "Pafos", "לימסול", "Limassol",
           "איה נאפה", "Ayia Napa", "ניקוסיה", "Nicosia", "פרוטרס", "Protaras",
           "קירניה", "Kyrenia", "פמגוסטה", "Famagusta"),
    "CZ": ("פראג", "Prague", "Praha", "ברנו", "Brno", "קרלובי וארי", "Karlovy Vary",
           "צ'סקי קרומלוב", "Cesky Krumlov"),
    "DE": ("ברלין", "Berlin", "מינכן", "Munich", "פרנקפורט", "Frankfurt", "המבורג", "Hamburg",
           "דיסלדורף", "Dusseldorf", "קלן", "Cologne", "Koln", "שטוטגרט", "Stuttgart",
           "באדן באדן", "Baden-Baden", "לייפציג", "Leipzig", "דרזדן", "Dresden",
           "נירנברג", "Nuremberg", "ברמן", "Bremen", "הנובר", "Hannover", "Hanover",
           "היער השחור", "Black Forest", "בוואריה", "Bavaria", "ממינגן", "Memmingen"),
    "DK": ("קופנהגן", "Copenhagen", "בילונד", "Billund"),
    "EE": ("טאלין", "Tallinn"),
    "ES": ("ברצלונה", "Barcelona", "מדריד", "Madrid", "מלגה", "Malaga", "ולנסיה", "Valencia",
           "סביליה", "Seville", "Sevilla", "איביזה", "Ibiza", "מיורקה", "מאיורקה", "Mallorca",
           "Majorca", "פלמה", "Palma", "טנריף", "טנריפה", "Tenerife", "האיים הקנריים",
           "Canary Islands", "גרנדה", "Granada", "אליקנטה", "Alicante", "ביבלאו", "בילבאו", "Bilbao",
           "גרן קנריה", "Gran Canaria", "מנורקה", "Menorca", "קוסטה בראווה", "Costa Brava",
           "קוסטה דל סול", "Costa del Sol", "ג'ירונה", "Girona", "מרבייה", "Marbella"),
    "FI": ("הלסינקי", "Helsinki", "רובניימי", "Rovaniemi", "לפלנד", "Lapland", "קיטילה", "Kittila",
           "לוי", "Levi", "סאריסלקה", "Saariselka", "איבלו", "Ivalo"),
    "FR": ("פריז", "Paris", "ניס", "Nice", "מרסיי", "Marseille", "ליון", "Lyon", "בורדו", "Bordeaux",
           "טולוז", "Toulouse", "קאן", "Cannes", "שאמוני", "Chamonix", "קורסיקה", "Corsica",
           "הריביירה הצרפתית", "French Riviera", "קוט דאזור", "Cote d'Azur", "פרובאנס", "Provence",
           "אלזס", "Alsace", "שטרסבורג", "Strasbourg", "מונפלייה", "Montpellier",
           "האלפים הצרפתיים", "French Alps", "וואל טורנס", "Val Thorens"),
    "GB": ("לונדון", "London", "מנצ'סטר", "Manchester", "אדינבורו", "אדינבורג", "Edinburgh",
           "ליברפול", "Liverpool", "גלזגו", "Glasgow", "בירמינגהאם", "Birmingham"),
    "GE": ("טביליסי", "Tbilisi", "בטומי", "Batumi", "גודאורי", "Gudauri", "קוטאיסי", "Kutaisi",
           "בקוריאני", "Bakuriani", "קזבגי", "Kazbegi", "בורג'ומי", "Borjomi"),
    "GR": ("אתונה", "Athens", "כרתים", "Crete", "הרקליון", "Heraklion", "חאניה", "Chania",
           "רודוס", "Rhodes", "קורפו", "Corfu", "סלוניקי", "Thessaloniki", "זקינתוס", "Zakynthos",
           "Zante", "סנטוריני", "Santorini", "מיקונוס", "Mykonos", "קוס", "Kos", "כלקידיקי",
           "חלקידיקי", "Halkidiki", "Chalkidiki", "קפלוניה", "Kefalonia", "Cephalonia",
           "פרבזה", "Preveza", "נקסוס", "נאקסוס", "Naxos", "פארוס", "פרוס", "Paros",
           "קבאלה", "Kavala", "יואנינה", "Ioannina", "לפקדה", "Lefkada", "סקיאתוס", "Skiathos",
           "תסוס", "Thassos", "פלופונס", "Peloponnese", "קלמטה", "Kalamata", "לוטרקי", "Loutraki",
           "האיים היווניים", "Greek Islands", "אקרופוליס", "Acropolis"),
    "HR": ("זאגרב", "Zagreb", "דוברובניק", "Dubrovnik", "ספליט", "Split", "פולה", "Pula",
           "זאדאר", "Zadar", "איסטריה", "Istria"),
    "HU": ("בודפשט", "Budapest", "דברצן", "Debrecen", "הוויז", "Heviz", "Hévíz", "בלטון", "Balaton"),
    "IE": ("דבלין", "Dublin"),
    "IS": ("רייקיאוויק", "רייקיאביק", "Reykjavik"),
    "IT": ("רומא", "Rome", "Roma", "מילאנו", "Milan", "Milano", "ונציה", "Venice", "Venezia",
           "נאפולי", "Naples", "Napoli", "פירנצה", "Florence", "Firenze", "סיציליה", "Sicily",
           "פלרמו", "Palermo", "קטניה", "Catania", "בולוניה", "Bologna", "ורונה", "Verona",
           "טורינו", "Turin", "Torino", "בארי", "Bari", "סרדיניה", "Sardinia", "קליארי", "Cagliari",
           "אולביה", "Olbia", "פיזה", "Pisa", "ברגמו", "Bergamo", "טוסקנה", "Tuscany",
           "אגם גארדה", "Lake Garda", "אגם קומו", "Lake Como", "דולומיטים", "Dolomites",
           "אמלפי", "Amalfi", "חוף אמלפי", "Amalfi Coast", "קפרי", "Capri", "ג'נובה", "Genoa",
           "טרוויזו", "Treviso", "ברינדיזי", "Brindisi", "פוליה", "Puglia", "Apulia",
           "קלבריה", "Calabria", "טרפאני", "Trapani", "ריבה דל גארדה", "Riva del Garda",
           "ליבינו", "Livigno", "צרוויניה", "Cervinia", "אנקונה", "Ancona", "פרמה", "Parma",
           "פסקרה", "Pescara", "למפדוזה", "Lampedusa", "ליגוריה", "Liguria"),
    "LT": ("וילנה", "וילנייוס", "Vilnius", "קובנה", "Kaunas"),
    "LU": ("לוקסמבורג", "Luxembourg"),
    "LV": ("ריגה", "Riga", "יורמלה", "Jurmala"),
    "MC": ("מונטה קרלו", "Monte Carlo"),
    "MD": ("קישינב", "קישינאו", "Chisinau", "Kishinev"),
    "ME": ("טיבאט", "Tivat", "פודגוריצה", "Podgorica", "בודווה", "Budva", "קוטור", "Kotor"),
    "MK": ("סקופיה", "Skopje", "אוחריד", "Ohrid"),
    "MT": ("ולטה", "Valletta", "גוזו", "Gozo"),
    "NL": ("אמסטרדם", "Amsterdam", "רוטרדם", "Rotterdam", "איינדהובן", "Eindhoven",
           "האג", "The Hague"),
    "NO": ("אוסלו", "Oslo", "טרומסו", "Tromso", "Tromsø", "ברגן", "Bergen", "פיורדים", "Fjords"),
    "PL": ("ורשה", "Warsaw", "קרקוב", "Krakow", "Kraków", "קטוביץ", "קטוביצה", "Katowice",
           "גדנסק", "Gdansk", "ורוצלב", "Wroclaw", "זקופנה", "Zakopane", "פוזנן", "Poznan",
           "לודז'", "Lodz", "לובלין", "Lublin", "רז'שוב", "Rzeszow"),
    "PT": ("ליסבון", "Lisbon", "Lisboa", "פורטו", "Porto", "מדיירה", "מדירה", "Madeira",
           "פונשל", "Funchal", "אזורים", "Azores", "אלגרבה", "Algarve", "פארו", "Faro"),
    "RO": ("בוקרשט", "Bucharest", "קלוז'", "Cluj", "Cluj-Napoca", "ברשוב", "Brasov",
           "טימישוארה", "Timisoara", "יאשי", "Iasi", "סיביו", "Sibiu", "קונסטנצה", "Constanta",
           "פויאנה ברשוב", "Poiana Brasov", "טרנסילבניה", "Transylvania"),
    "RS": ("בלגרד", "Belgrade", "נובי סאד", "Novi Sad", "נִיש", "Nis", "קופאוניק", "Kopaonik"),
    "RU": ("מוסקבה", "Moscow", "סנט פטרסבורג", "St Petersburg", "Saint Petersburg", "סוצ'י", "Sochi"),
    "SE": ("שטוקהולם", "Stockholm", "גטבורג", "Gothenburg", "Goteborg"),
    "SI": ("לובליאנה", "Ljubljana", "בלד", "Bled"),
    "SK": ("ברטיסלבה", "Bratislava", "קושיצה", "Kosice", "טטרה", "Tatras"),
    "TR": ("איסטנבול", "Istanbul", "אנטליה", "Antalya", "בודרום", "Bodrum", "דלמן", "Dalaman",
           "איזמיר", "Izmir", "קפדוקיה", "Cappadocia", "טרבזון", "Trabzon", "מרמריס", "Marmaris",
           "אלניה", "Alanya", "קמר", "Kemer", "בלק", "Belek", "פתחיה", "Fethiye", "אנקרה", "Ankara",
           "קושאדסי", "Kusadasi", "צ'שמה", "Cesme"),
    "UA": ("קייב", "קיוב", "Kyiv", "Kiev", "אודסה", "Odessa", "לבוב", "Lviv", "אומן", "Uman"),
    # ---- Asia ----
    "AE": ("דובאי", "Dubai", "אבו דאבי", "אבו דהבי", "Abu Dhabi", "ראס אל חיימה",
           "ראס אל-חיימה", "Ras Al Khaimah", "שארג'ה", "Sharjah", "עג'מאן", "Ajman",
           "פוג'יירה", "Fujairah"),
    "BH": ("מנאמה", "Manama"),
    "CN": ("בייג'ינג", "Beijing", "שנגחאי", "Shanghai", "גואנגג'ואו", "Guangzhou",
           "שנזן", "Shenzhen", "שיאן", "Xi'an", "הייקו", "Haikou", "סניה", "Sanya"),
    "HK": ("Hong Kong",),
    "ID": ("באלי", "Bali", "ג'קרטה", "Jakarta", "דנפסר", "Denpasar", "לומבוק", "Lombok"),
    "IN": ("דלהי", "Delhi", "ניו דלהי", "New Delhi", "מומבאי", "Mumbai", "Bombay", "גואה", "Goa",
           "בנגלור", "Bangalore", "Bengaluru", "קוצ'ין", "Kochi", "Cochin", "קרלה", "Kerala",
           "ג'איפור", "Jaipur", "אגרה", "Agra", "קולקטה", "Kolkata", "צ'נאי", "Chennai",
           "מנאלי", "Manali", "דרמסאלה", "Dharamsala", "רישיקש", "Rishikesh", "לדאק", "Ladakh"),
    "JO": ("עקבה", "Aqaba", "עמאן", "Amman", "פטרה", "Petra", "ים המלח הירדני", "ואדי רם", "Wadi Rum"),
    "JP": ("טוקיו", "Tokyo", "אוסקה", "אוסאקה", "Osaka", "קיוטו", "Kyoto", "הירושימה", "Hiroshima",
           "סאפורו", "Sapporo", "נגויה", "Nagoya", "פוקואוקה", "Fukuoka", "אוקינאווה", "Okinawa",
           "נארה", "Nara", "הוקאידו", "Hokkaido", "נריטה", "Narita", "הנדה", "Haneda"),
    "KG": ("בישקק", "Bishkek"),
    "KH": ("פנום פן", "Phnom Penh", "סיאם ריפ", "Siem Reap"),
    "KR": ("סיאול", "Seoul", "בוסאן", "Busan", "ג'ג'ו", "Jeju"),
    "KZ": ("אלמטי", "Almaty", "אסטנה", "Astana"),
    "LA": ("ויינטיאן", "Vientiane", "לואנג פרבאנג", "Luang Prabang"),
    "LK": ("קולומבו", "Colombo", "קנדי", "Kandy"),
    "MN": ("אולן בטור", "Ulaanbaatar"),
    "MV": ("מאלה", "Male", "Malé"),
    "MY": ("קואלה לומפור", "Kuala Lumpur", "לנגקאווי", "Langkawi", "פנאנג", "Penang"),
    "NP": ("קטמנדו", "Kathmandu", "פוקרה", "Pokhara"),
    "OM": ("מוסקט", "Muscat", "צלאלה", "Salalah"),
    "PH": ("מנילה", "Manila", "סבו", "Cebu", "פלאוואן", "Palawan", "בורקאי", "Boracay"),
    "QA": ("דוחה", "Doha"),
    "SA": ("ריאד", "Riyadh", "ג'דה", "Jeddah"),
    "SG": (),
    "TH": ("בנגקוק", "Bangkok", "פוקט", "Phuket", "קו סמוי", "קוסמוי", "קו סאמוי", "Koh Samui",
           "Ko Samui", "Samui", "סמוי", "צ'יאנג מאי", "צ'אנג מאי", "Chiang Mai", "צ'יאנג ראי",
           "Chiang Rai", "קראבי", "Krabi", "פטאיה", "Pattaya", "קו פנגן", "Koh Phangan",
           "קו טאו", "Koh Tao", "הואה הין", "Hua Hin", "קו צ'אנג", "Koh Chang", "קו לנטה",
           "Koh Lanta", "קו פי פי", "Koh Phi Phi", "קו ליפה", "Koh Lipe", "קאו לאק", "Khao Lak"),
    "TW": ("טאיפיי", "טייפה", "Taipei"),
    "UZ": ("טשקנט", "Tashkent", "סמרקנד", "Samarkand", "בוכרה", "Bukhara", "חיווה", "Khiva"),
    "VN": ("האנוי", "Hanoi", "הו צ'י מין", "הו צ'י מין סיטי", "Ho Chi Minh", "Ho Chi Minh City",
           "סייגון", "Saigon", "דה נאנג", "דנאנג", "Da Nang", "Danang", "פו קוק", "פוקוק", "Phu Quoc",
           "נה טראנג", "נטראנג", "Nha Trang", "הוי אן", "Hoi An", "הא לונג", "Ha Long", "Halong",
           "סאפה", "Sapa", "דאלאט", "Dalat", "קאם ראן", "Cam Ranh"),
    # ---- North America ----
    "BS": ("נסאו", "Nassau"),
    "CA": ("טורונטו", "Toronto", "מונטריאול", "Montreal", "ונקובר", "Vancouver", "קלגרי", "Calgary",
           "אוטווה", "Ottawa", "קוויבק", "Quebec", "בנף", "Banff"),
    "CR": ("סן חוזה", "San Jose"),
    "CU": ("הוואנה", "הבנה", "Havana", "ורדרו", "Varadero"),
    "DO": ("פונטה קאנה", "Punta Cana", "סנטו דומינגו", "Santo Domingo"),
    "JM": ("מונטגו ביי", "Montego Bay", "קינגסטון", "Kingston"),
    "MX": ("קנקון", "Cancun", "מקסיקו סיטי", "Mexico City", "פלאיה דל כרמן", "Playa del Carmen",
           "טולום", "Tulum", "לוס קאבוס", "Los Cabos"),
    "PA": ("פנמה סיטי", "Panama City"),
    "US": ("ניו יורק", "New York", "NYC", "לוס אנג'לס", "לוס אנגלס", "Los Angeles", "LA",
           "מיאמי", "Miami", "לאס וגאס", "Las Vegas", "סן פרנסיסקו", "San Francisco",
           "אורלנדו", "Orlando", "בוסטון", "Boston", "וושינגטון", "Washington", "שיקגו", "Chicago",
           "הוואי", "Hawaii", "הונולולו", "Honolulu", "מאווי", "Maui", "פילדלפיה", "Philadelphia",
           "אטלנטה", "Atlanta", "סיאטל", "Seattle", "ניו ג'רזי", "New Jersey", "ניוארק", "Newark",
           "פלורידה", "Florida", "קליפורניה", "California", "ניו אורלינס", "New Orleans",
           "סן דייגו", "San Diego", "דאלאס", "Dallas", "יוסטון", "Houston", "דנבר", "Denver",
           "פורט לודרדייל", "Fort Lauderdale", "טמפה", "Tampa"),
    # ---- South America ----
    "AR": ("בואנוס איירס", "Buenos Aires", "פטגוניה", "Patagonia", "בארילוצ'ה", "Bariloche",
           "מנדוסה", "Mendoza", "אושואיה", "Ushuaia"),
    "BO": ("לה פאס", "La Paz"),
    "BR": ("ריו דה ז'ניירו", "ריו", "Rio de Janeiro", "Rio", "סאו פאולו", "Sao Paulo", "São Paulo",
           "סלבדור", "Salvador", "פלוריאנופוליס", "Florianopolis", "איגואסו", "Iguazu"),
    "CL": ("סנטיאגו", "Santiago"),
    "CO": ("בוגוטה", "Bogota", "Bogotá", "קרטחנה", "Cartagena", "מדיין", "Medellin"),
    "EC": ("קיטו", "Quito", "גלאפגוס", "Galapagos"),
    "PE": ("לימה", "Lima", "קוסקו", "Cusco", "Cuzco", "מאצ'ו פיצ'ו", "Machu Picchu"),
    "UY": ("מונטווידאו", "Montevideo"),
    # ---- Africa ----
    "EG": ("שארם א-שייח", "שארם א שייח", "שארם אל שייח", "שארם", "Sharm el-Sheikh",
           "Sharm El Sheikh", "Sharm", "טאבה", "Taba", "קהיר", "Cairo", "הורגדה", "Hurghada",
           "דהב", "Dahab", "נואיבה", "Nuweiba", "אלכסנדריה", "Alexandria", "לוקסור", "Luxor",
           "אסואן", "Aswan", "מרסה עלם", "Marsa Alam", "חצי האי סיני"),
    "ET": ("אדיס אבבה", "Addis Ababa", "גונדר", "Gondar"),
    "KE": ("ניירובי", "Nairobi", "מומבסה", "Mombasa", "מסאי מארה", "Masai Mara"),
    "MA": ("מרקש", "Marrakech", "Marrakesh", "קזבלנקה", "Casablanca", "פאס", "Fes", "Fez",
           "טנג'יר", "Tangier", "אגאדיר", "Agadir", "רבאט", "Rabat"),
    "MG": ("אנטננריבו", "Antananarivo", "נוסי בה", "Nosy Be"),
    "MU": ("פורט לואי", "Port Louis"),
    "NA": ("וינדהוק", "Windhoek"),
    "RW": ("קיגאלי", "Kigali"),
    "SC": ("מאהה", "האי מאהה", "Mahe", "Mahé", "פראסלין", "Praslin", "לה דיג", "La Digue"),
    "TN": ("ג'רבה", "Djerba", "תוניס", "Tunis"),
    "TZ": ("זנזיבר", "Zanzibar", "דאר א-סלאם", "Dar es Salaam", "קילימנג'רו", "Kilimanjaro",
           "ארושה", "Arusha", "סרנגטי", "Serengeti"),
    "UG": ("קמפלה", "Kampala", "אנטבה", "Entebbe"),
    "ZA": ("קייפטאון", "קייפ טאון", "Cape Town", "יוהנסבורג", "Johannesburg", "דרבן", "Durban",
           "קרוגר", "Kruger"),
    # ---- Oceania ----
    "AU": ("סידני", "Sydney", "מלבורן", "Melbourne", "בריסביין", "Brisbane", "פרת'", "Perth",
           "אדלייד", "Adelaide", "קיירנס", "Cairns", "גולד קוסט", "Gold Coast"),
    "FJ": ("נאדי", "Nadi", "סובה", "Suva"),
    "NZ": ("אוקלנד", "Auckland", "וולינגטון", "Wellington", "קווינסטאון", "Queenstown",
           "קרייסטצ'רץ'", "Christchurch"),
    "PF": ("בורה בורה", "Bora Bora"),
    # ---- Home ----
    "IL": ("אילת", "Eilat", "רמון", "Ramon", "תל אביב", "Tel Aviv"),
}

_ISO_ALIASES = {"UK": "GB"}
_SPLIT = re.compile(r"\s*(?:[|,/()\[\]:;]|\s[-–—]\s|-)\s*")
_HE_PREFIXES = "בלמוהש"   # "בפראג", "לרומא"... one-letter Hebrew prefixes


def _key(text: str) -> str:
    """Spelling-insensitive key: no accents, punctuation, spaces or doubled yod/vav."""
    s = unicodedata.normalize("NFKD", str(text or "")).lower()
    s = "".join(ch for ch in s if ch.isalnum())
    # Hebrew plene spelling varies ("אזרבייגן" / "אזרביגן"); final letters too.
    s = s.replace("יי", "י").replace("וו", "ו")
    return s.translate(str.maketrans("ךםןףץ", "כמנפצ"))


_COUNTRY_INDEX: dict[str, str] = {}
_PLACE_INDEX: dict[str, str] = {}


def _build() -> None:
    for code, (english, _continent, *names) in _COUNTRIES.items():
        for name in (code, english, *names):
            _COUNTRY_INDEX.setdefault(_key(name), code)
    for code, places in _PLACES.items():
        for name in places:
            _PLACE_INDEX.setdefault(_key(name), code)
    # Country names also work as a destination ("זנזיבר" is in places,
    # "תאילנד" as a destination falls back to the country).
    for k, code in _COUNTRY_INDEX.items():
        if len(k) > 2:
            _PLACE_INDEX.setdefault(k, code)


_build()
_MAX_WORDS = max(len(n.split()) for names in _PLACES.values() for n in names)


def country_name(code: str) -> str:
    """ISO-2 -> English country name ('' when unknown)."""
    entry = _COUNTRIES.get((code or "").upper())
    return entry[0] if entry else ""


def country_name_he(country: str) -> str:
    """ISO-2 code or country name -> Hebrew country name ('' when unknown)."""
    code = lookup_country(country)
    return _COUNTRIES[code][2] if code else ""


def continent_of(code: str) -> Optional[str]:
    entry = _COUNTRIES.get((code or "").upper())
    return entry[1] if entry else None


def lookup_country(country: str) -> Optional[str]:
    """Country as written by a site (English, ISO-2 or Hebrew) -> ISO-2, or None."""
    text = (country or "").strip()
    if not text:
        return None
    if len(text) == 2 and text.isascii():
        code = _ISO_ALIASES.get(text.upper(), text.upper())
        return code if code in _COUNTRIES else None
    k = _key(text)
    if len(k) <= 2:   # a 2-letter key only means something as an ISO code (above)
        return None
    return _COUNTRY_INDEX.get(k)


def _lookup_place(text: str) -> Optional[str]:
    k = _key(text)
    if len(k) < 2:
        return None
    hit = _PLACE_INDEX.get(k)
    if hit:
        return hit
    # Hebrew one-letter prefix: "בפראג" -> "פראג"
    if k[0] in _HE_PREFIXES and len(k) > 3:
        return _PLACE_INDEX.get(k[1:])
    return None


def lookup_place(destination: str) -> Optional[str]:
    """Destination as written by a site -> ISO-2, or None.

    Tries the whole text, then each part between separators, then runs of
    1..N words inside it (longest first), so "סיציליה (פלרמו)",
    "טורונטו - ON" and "מאורגן: 5 ימים בפראג" all resolve.
    """
    text = (destination or "").strip()
    if not text:
        return None
    hit = _lookup_place(text)
    if hit:
        return hit
    parts = [p for p in _SPLIT.split(text) if p and p.strip()]
    for part in parts:
        hit = _lookup_place(part)
        if hit:
            return hit
    words = re.findall(r"[\w'\"׳״.-]+", text)
    for n in range(min(_MAX_WORDS, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            if n == 1 and len(_key(chunk)) < 3:
                continue   # "LA", "ON"... too short to trust inside a longer title
            hit = _lookup_place(chunk)
            if hit:
                return hit
    return None


def classify(destination: str, country: str = "") -> Optional[tuple[str, str]]:
    """(ISO-2 country code, continent) for a deal, or None when unknown.

    The site's country wins when it is recognised; otherwise the destination
    text decides.
    """
    code = lookup_country(country) or lookup_place(destination)
    if not code:
        return None
    return code, _COUNTRIES[code][1]
