<?php
/**
 * fetch_new.php
 * Hämtar nya ärenden från Skolinspektionens sökportal sedan senaste datumet i data.json.
 * Anropas via POST från index.html-knappen "Hämta nya ärenden".
 */
set_time_limit(120);
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');

const SEARCH_URL = 'https://externsearchport.skolinspektionen.se/search.aspx?view=ac';
const DATA_FILE  = __DIR__ . '/data.json';

// ── Ladda befintlig data ──
if (!file_exists(DATA_FILE)) {
    http_response_code(500);
    echo json_encode(['error' => 'data.json saknas']);
    exit;
}
$existing    = json_decode(file_get_contents(DATA_FILE), true) ?: [];
$existingMap = array_flip(array_column($existing, 'dno'));  // dno -> index, för snabb dublettkoll

// ── Hitta senaste datum i befintlig data ──
$maxDate = '2019-01-01';
foreach ($existing as $r) {
    if (!empty($r['date']) && $r['date'] > $maxDate) $maxDate = $r['date'];
}
// Börja från samma dag (fångar ev. ärenden som saknades) men filtrera bort kända dno:n
$fromDate = $maxDate;
$toDate   = date('Y-m-d');

// ── Scrapa ──
$scraped = [];
scrapeRange($fromDate, $toDate, $scraped, 0);

// ── Lägg till enbart genuint nya ──
$newRecords = [];
foreach ($scraped as $r) {
    if (!isset($existingMap[$r['dno']])) {
        $newRecords[]          = $r;
        $existingMap[$r['dno']] = true;
    }
}

// ── Spara om det finns nyheter ──
if (count($newRecords) > 0) {
    $merged = array_merge($existing, $newRecords);
    // BOM-fri UTF-8
    file_put_contents(DATA_FILE, json_encode($merged,
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
}

echo json_encode([
    'status'    => 'ok',
    'new'       => count($newRecords),
    'from'      => $fromDate,
    'to'        => $toDate,
    'total'     => count($existing) + count($newRecords),
]);


// ════════════════════════════════════════════════
//  Hjälpfunktioner
// ════════════════════════════════════════════════

/**
 * Rekursiv datumdelning om resultatet överstiger 1000 poster.
 */
function scrapeRange(string $from, string $to, array &$results, int $depth): void {
    if ($depth > 8) return;

    $html = doSearch($from, $to);
    if ($html === null) return;

    // Om fler än 1000 träffar — dela intervallet
    if (mb_strpos($html, 'mer än 1000') !== false || mb_strpos($html, 'Visar 1000') !== false) {
        $midTs = (int)(((int)strtotime($from) + (int)strtotime($to)) / 2);
        $mid   = date('Y-m-d', $midTs);
        if ($mid === $from || $mid === $to) {
            parseTable($html, $results);  // kan inte dela mer, ta vad vi har
            return;
        }
        scrapeRange($from, $mid, $results, $depth + 1);
        scrapeRange(date('Y-m-d', strtotime($mid . ' +1 day')), $to, $results, $depth + 1);
        return;
    }

    parseTable($html, $results);
}

/**
 * Hämtar sökresultat för ett datumintervall. Hanterar ASP.NET ViewState/EventValidation.
 */
function doSearch(string $from, string $to): ?string {
    static $cookieFile = null;
    if ($cookieFile === null) $cookieFile = tempnam(sys_get_temp_dir(), 'si_');

    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL            => SEARCH_URL,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_COOKIEJAR      => $cookieFile,
        CURLOPT_COOKIEFILE     => $cookieFile,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_USERAGENT      => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_ENCODING       => 'gzip, deflate',
    ]);

    // Steg 1: GET för att hämta ViewState mm.
    $getHtml = curl_exec($ch);
    if (!$getHtml) { curl_close($ch); return null; }

    preg_match('/id="__VIEWSTATE"\s+value="([^"]*)"/',          $getHtml, $vs);
    preg_match('/id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"/', $getHtml, $vsg);
    preg_match('/id="__EVENTVALIDATION"\s+value="([^"]*)"/',    $getHtml, $ev);
    if (empty($vs[1])) { curl_close($ch); return null; }

    // Steg 2: POST med sökparametrar
    $post = http_build_query([
        '__VIEWSTATE'          => $vs[1],
        '__VIEWSTATEGENERATOR' => $vsg[1] ?? '',
        '__EVENTVALIDATION'    => $ev[1] ?? '',
        'txtFromDate'          => $from,
        'txtToDate'            => $to,
        'ddlDiaries'           => '',
        'btnSearch'            => 'Sök',
    ]);

    curl_setopt_array($ch, [
        CURLOPT_POST       => true,
        CURLOPT_POSTFIELDS => $post,
    ]);

    $result = curl_exec($ch);
    curl_close($ch);
    return $result ?: null;
}

/**
 * Parsar HTML-tabellen och lägger till poster i $results.
 */
function parseTable(string $html, array &$results): void {
    $dom = new DOMDocument('1.0', 'utf-8');
    libxml_use_internal_errors(true);
    $dom->loadHTML('<?xml encoding="UTF-8">' . $html);
    libxml_clear_errors();

    $xpath = new DOMXPath($dom);
    // Prova GridView1 (sökresultattabellen)
    $rows = $xpath->query('//*[@id="GridView1"]/tr[position()>1]');
    if (!$rows || $rows->length === 0) return;

    foreach ($rows as $row) {
        $cells = $row->getElementsByTagName('td');
        if ($cells->length < 4) continue;

        $dno     = cleanStr($cells->item(0)->textContent);
        $date    = cleanStr($cells->item(1)->textContent);
        $subject = cleanStr($cells->item(2)->textContent);
        $typ     = cleanStr($cells->item(3)->textContent);

        if (empty($dno) || !preg_match('/^SI\s*\d/', $dno)) continue;

        // Normalisera datum (YYYY-MM-DD)
        if (preg_match('/^(\d{2})-(\d{2})-(\d{4})$/', $date, $m)) {
            $date = "$m[3]-$m[2]-$m[1]";
        }

        $results[] = [
            'dno'     => $dno,
            'date'    => $date,
            'subject' => $subject,
            'typ'     => $typ,
            'kommun'  => extractKommun($subject . ' ' . $typ),
        ];
    }
}

function cleanStr(string $s): string {
    return trim(preg_replace('/[\x00-\x08\x0b-\x1f\x7f]/u', ' ', $s));
}

/**
 * Extraherar kommunnamn från fritext.
 * Söker (1) "i X kommun"-mönster, sedan (2) kändakommunnamn (längst först).
 */
function extractKommun(string $text): string {
    static $kommunList = null;
    if ($kommunList === null) {
        $kommunList = ['Ale','Alingsås','Alvesta','Aneby','Arboga','Arjeplog','Arvidsjaur',
            'Arvika','Askersund','Avesta','Bengtsfors','Berg','Bjurholm','Bjuv','Boden',
            'Bollebygd','Bollnäs','Borgholm','Borlänge','Borås','Botkyrka','Boxholm',
            'Bromölla','Bräcke','Burlöv','Båstad','Dals-Ed','Danderyd','Degerfors',
            'Dorotea','Eda','Ekerö','Eksjö','Emmaboda','Enköping','Eskilstuna','Eslöv',
            'Essunga','Fagersta','Falkenberg','Falköping','Falun','Filipstad','Finspång',
            'Flen','Forshaga','Färgelanda','Gagnef','Gislaved','Gnesta','Gnosjö','Gotland',
            'Grums','Grästorp','Gullspång','Gällivare','Gävle','Göteborg','Götene','Habo',
            'Hagfors','Hallsberg','Hallstahammar','Halmstad','Hammarö','Haninge','Haparanda',
            'Heby','Hedemora','Helsingborg','Herrljunga','Hjo','Hofors','Huddinge',
            'Hudiksvall','Hultsfred','Hylte','Håbo','Hällefors','Härjedalen','Härnösand',
            'Härryda','Hässleholm','Höganäs','Högsby','Hörby','Höör','Jokkmokk','Järfälla',
            'Jönköping','Kalix','Kalmar','Karlsborg','Karlshamn','Karlskoga','Karlskrona',
            'Karlstad','Katrineholm','Kil','Kinda','Kiruna','Klippan','Knivsta','Kramfors',
            'Kristianstad','Kristinehamn','Krokom','Kumla','Kungsbacka','Kungsör','Kungälv',
            'Kävlinge','Köping','Laholm','Landskrona','Laxå','Lekeberg','Leksand','Lerum',
            'Lessebo','Lidingö','Lidköping','Lilla Edet','Lindesberg','Linköping','Ljungby',
            'Ljusdal','Ljusnarsberg','Lomma','Ludvika','Luleå','Lund','Lycksele','Lysekil',
            'Malmö','Malung-Sälen','Malå','Mariestad','Mark','Markaryd','Mellerud','Mjölby',
            'Mora','Motala','Mullsjö','Munkedal','Munkfors','Mölndal','Mönsterås','Mörbylånga',
            'Nacka','Nora','Norberg','Nordanstig','Nordmaling','Norrköping','Norrtälje',
            'Norsjö','Nybro','Nykvarn','Nyköping','Nynäshamn','Nässjö','Ockelbo','Olofström',
            'Orsa','Orust','Osby','Oskarshamn','Ovanåker','Oxelösund','Pajala','Partille',
            'Perstorp','Piteå','Ragunda','Robertsfors','Ronneby','Rättvik','Sala','Salem',
            'Sandviken','Sigtuna','Simrishamn','Sjöbo','Skara','Skellefteå','Skinnskatteberg',
            'Skurup','Skövde','Smedjebacken','Sollefteå','Sollentuna','Solna','Sorsele',
            'Sotenäs','Staffanstorp','Stenungsund','Stockholm','Storfors','Storuman',
            'Strängnäs','Strömstad','Strömsund','Sundbyberg','Sundsvall','Sunne','Surahammar',
            'Svalöv','Svedala','Svenljunga','Säffle','Säter','Sävsjö','Söderhamn',
            'Söderköping','Södertälje','Sölvesborg','Tanum','Tibro','Tidaholm','Tierp',
            'Timrå','Tingsryd','Tjörn','Tomelilla','Torsby','Torsås','Tranemo','Tranås',
            'Trelleborg','Trollhättan','Trosa','Tyresö','Täby','Töreboda','Uddevalla',
            'Ulricehamn','Umeå','Upplands-Bro','Upplands Väsby','Uppsala','Uppvidinge',
            'Vadstena','Vaggeryd','Valdemarsvik','Vallentuna','Vansbro','Vara','Varberg',
            'Vaxholm','Vellinge','Vetlanda','Vilhelmina','Vimmerby','Vindeln','Vingåker',
            'Vårgårda','Vänersborg','Vännäs','Värmdö','Värnamo','Västervik','Västerås',
            'Växjö','Ydre','Ystad','Åmål','Ånge','Åre','Årjäng','Åsele','Åstorp',
            'Åtvidaberg','Älmhult','Älvdalen','Älvkarleby','Älvsbyn','Ängelholm','Öckerö',
            'Ödeshög','Örebro','Örkelljunga','Örnsköldsvik','Östersund','Österåker',
            'Östhammar','Östra Göinge','Överkalix','Övertorneå'];
        usort($kommunList, fn($a, $b) => strlen($b) - strlen($a));
    }

    // Mönster 1: "i X kommun"
    if (preg_match('/\bi\s+([\wÅÄÖåäö][\wÅÄÖåäö\s\-]*?)\s+kommun\b/iu', $text, $m)) {
        $cand = trim($m[1]);
        foreach ($kommunList as $k) {
            if (mb_strtolower($k) === mb_strtolower($cand)) return $k;
        }
    }

    // Mönster 2: känd kommunnamn förekommer i texten
    foreach ($kommunList as $k) {
        if (mb_stripos($text, $k) !== false) return $k;
    }

    return '';
}
