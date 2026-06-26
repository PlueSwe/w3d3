<?php
/**
 * fetch_case.php
 * Hämtar ärendedetaljer och beslutslänk från Skolinspektionens portal.
 * Anropas med GET: ?dno=SI+2026:1234
 */
header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');

$dno = trim($_GET['dno'] ?? '');
if (!$dno || !preg_match('/^SI\s*\d{4}:\d+$/i', $dno)) {
    http_response_code(400);
    echo json_encode(['error' => 'Ogiltigt ärendenummer']);
    exit;
}

// Extrahera "YYYY:NNNN"-delen (utan "SI ")
$caseRef = preg_replace('/^SI\s*/i', '', $dno);  // "2026:1234"

// ── Steg 1: Sök i externa portalen efter detta dno ──
$cookieFile = tempnam(sys_get_temp_dir(), 'si_case_');

$ch = curl_init();
curl_setopt_array($ch, [
    CURLOPT_URL            => 'https://externsearchport.skolinspektionen.se/search.aspx?view=ac',
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_COOKIEJAR      => $cookieFile,
    CURLOPT_COOKIEFILE     => $cookieFile,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_SSL_VERIFYPEER => false,
    CURLOPT_USERAGENT      => 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    CURLOPT_TIMEOUT        => 20,
]);
$getHtml = curl_exec($ch);

preg_match('/id="__VIEWSTATE"\s+value="([^"]*)"/',          $getHtml, $vs);
preg_match('/id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"/', $getHtml, $vsg);
preg_match('/id="__EVENTVALIDATION"\s+value="([^"]*)"/',    $getHtml, $ev);

$result = ['dno' => $dno, 'caseRef' => $caseRef, 'beslut' => [], 'portalInfo' => null];

if (!empty($vs[1])) {
    // Sök på exakt ärendereferens
    $post = http_build_query([
        '__VIEWSTATE'          => $vs[1],
        '__VIEWSTATEGENERATOR' => $vsg[1] ?? '',
        '__EVENTVALIDATION'    => $ev[1] ?? '',
        'txtArendenummer'      => $caseRef,
        'ddlDiaries'           => '',
        'btnSearch'            => 'Sök',
    ]);

    curl_setopt_array($ch, [
        CURLOPT_POST       => true,
        CURLOPT_POSTFIELDS => $post,
    ]);
    $searchHtml = curl_exec($ch);

    // Parsa eventuella detaljer från söktabellen
    if ($searchHtml) {
        $dom = new DOMDocument();
        libxml_use_internal_errors(true);
        $dom->loadHTML('<?xml encoding="UTF-8">' . $searchHtml);
        libxml_clear_errors();
        $xpath = new DOMXPath($dom);

        // Hämta alla celler i första matchande rad
        $rows = $xpath->query('//*[@id="GridView1"]/tr[position()>1]');
        foreach ($rows as $row) {
            $cells = $row->getElementsByTagName('td');
            if ($cells->length < 4) continue;
            $rowDno = trim($cells->item(0)->textContent);
            if (mb_stripos($rowDno, $caseRef) !== false) {
                $result['portalInfo'] = [
                    'dno'     => trim($cells->item(0)->textContent),
                    'date'    => trim($cells->item(1)->textContent),
                    'subject' => trim($cells->item(2)->textContent),
                    'typ'     => trim($cells->item(3)->textContent),
                ];
                // Kolla om det finns en länk i raden
                $links = $row->getElementsByTagName('a');
                foreach ($links as $link) {
                    $href = $link->getAttribute('href');
                    if ($href) $result['portalLinks'][] = $href;
                }
                break;
            }
        }
    }
}
curl_close($ch);
@unlink($cookieFile);

// ── Steg 2: Sök beslut på Skolinspektionens beslutssida ──
$year = substr($caseRef, 0, 4);
$beslutUrl = "https://www.skolinspektionen.se/beslut-rapporter/sok-beslut/?query=" . urlencode($dno);

// Hämta beslutssidan med söktermen
$ch2 = curl_init();
curl_setopt_array($ch2, [
    CURLOPT_URL            => $beslutUrl,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_SSL_VERIFYPEER => false,
    CURLOPT_USERAGENT      => 'Mozilla/5.0',
    CURLOPT_TIMEOUT        => 15,
]);
$beslutHtml = curl_exec($ch2);
curl_close($ch2);

// Sök efter SIRIS-länkar i svaret
if ($beslutHtml) {
    preg_match_all('/https?:\/\/siris\.skolverket\.se\/siris\/ris\.openfile\?docID=(\d+)/i', $beslutHtml, $sirisMatches);
    foreach (array_unique($sirisMatches[0]) as $url) {
        $result['beslut'][] = $url;
    }
    // Sök även efter direktlänkar till PDF
    preg_match_all('/href="([^"]*\.pdf[^"]*)"/i', $beslutHtml, $pdfMatches);
    foreach (array_unique($pdfMatches[1]) as $url) {
        if (mb_stripos($url, $caseRef) !== false || mb_stripos($url, str_replace(':', '-', $caseRef)) !== false) {
            $result['beslut'][] = $url;
        }
    }
}

// Fallback: Sök Google-stil via skolinspektionen.se
$result['searchUrl'] = 'https://www.skolinspektionen.se/beslut-rapporter/sok-beslut/?query=' . urlencode($dno);
$result['sirisSearchUrl'] = 'http://siris.skolverket.se/siris/ris.search?freetext=' . urlencode($dno);

echo json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
