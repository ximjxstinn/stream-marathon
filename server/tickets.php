<?php
/*  Stream Marathon – Ticket-System (Backend)
 *  Auf dein Webhosting hochladen, z. B. nach  https://ximjxstinn.de/marathon/tickets.php
 *  Dann in der App (Admin → 🎫 Tickets) deinen ADMIN-KEY eintragen.
 *  Die Tickets landen in  tickets.json  neben dieser Datei.
 */

// ====== EINSTELLUNGEN (hier anpassen) ======
$ADMIN_KEY       = 'HIER-EINEN-LANGEN-GEHEIMEN-KEY-EINTRAGEN';   // nur DU kennst den – kommt in die App
$DISCORD_WEBHOOK = '';   // optional: Discord-Webhook-URL -> du bekommst eine Nachricht bei neuen Tickets
$DATA_FILE       = __DIR__ . '/tickets.json';
// ===========================================

header('Content-Type: application/json; charset=utf-8');

function load($f){ if(!file_exists($f)) return array(); $d=json_decode(file_get_contents($f),true); return is_array($d)?$d:array(); }
function store($f,$d){ file_put_contents($f, json_encode($d, JSON_PRETTY_PRINT|JSON_UNESCAPED_UNICODE|JSON_UNESCAPED_SLASHES), LOCK_EX); }
function out($x){ echo json_encode($x, JSON_UNESCAPED_UNICODE|JSON_UNESCAPED_SLASHES); exit; }
function clip($s,$n){ $s=trim((string)$s); return mb_substr($s,0,$n); }
function notify($url,$text){ if(!$url) return; $c=curl_init($url); curl_setopt_array($c,array(CURLOPT_POST=>1,CURLOPT_HTTPHEADER=>array('Content-Type: application/json'),CURLOPT_POSTFIELDS=>json_encode(array('content'=>$text)),CURLOPT_RETURNTRANSFER=>1,CURLOPT_TIMEOUT=>5)); curl_exec($c); curl_close($c); }

$body = json_decode(file_get_contents('php://input'), true);
if(!is_array($body)) out(array('ok'=>false,'error'=>'kein gültiger Aufruf'));
$action = isset($body['action']) ? $body['action'] : '';

$tickets = load($DATA_FILE);

// ---- Streamer: eigene Tickets abrufen ----
if($action==='mine'){
  $inst = clip(@$body['installId'],64);
  $mine = array_values(array_filter($tickets, function($t) use($inst){ return $t['installId']===$inst; }));
  usort($mine, function($a,$b){ return $b['updated'] - $a['updated']; });
  out(array('ok'=>true,'tickets'=>$mine));
}

// ---- Streamer: neues Ticket ----
if($action==='create'){
  $inst = clip(@$body['installId'],64);
  $text = clip(@$body['text'],2000);
  if($inst==='' || $text==='') out(array('ok'=>false,'error'=>'Angaben fehlen'));
  $id = 'T'.substr(str_replace('.','',uniqid('',true)),0,12);
  $now = round(microtime(true)*1000);
  $cat = in_array(@$body['cat'],array('bug','frage','wunsch')) ? $body['cat'] : 'frage';
  $t = array('id'=>$id,'installId'=>$inst,'channel'=>clip(@$body['channel'],40),'cat'=>$cat,
             'status'=>'offen','created'=>$now,'updated'=>$now,'unreadDev'=>1,'unreadUser'=>0,
             'msgs'=>array(array('from'=>'user','text'=>$text,'t'=>$now)));
  $tickets[] = $t; store($DATA_FILE,$tickets);
  notify($DISCORD_WEBHOOK, "🎫 **Neues Ticket** (".$cat.") von **".($t['channel']?:'?')."**\n> ".str_replace("\n"," ",$text));
  out(array('ok'=>true,'id'=>$id));
}

// ---- Streamer: Nachricht an bestehendes Ticket ----
if($action==='send'){
  $inst = clip(@$body['installId'],64); $id = clip(@$body['id'],20); $text = clip(@$body['text'],2000);
  foreach($tickets as &$t){ if($t['id']===$id && $t['installId']===$inst){
    $now = round(microtime(true)*1000);
    $t['msgs'][] = array('from'=>'user','text'=>$text,'t'=>$now);
    $t['updated']=$now; $t['status']='offen'; $t['unreadDev']=1;
    store($DATA_FILE,$tickets);
    notify($DISCORD_WEBHOOK, "💬 **".($t['channel']?:'?')."** (Ticket ".$id."):\n> ".str_replace("\n"," ",$text));
    out(array('ok'=>true));
  }}
  out(array('ok'=>false,'error'=>'Ticket nicht gefunden'));
}

// ---- ab hier: nur Entwickler (Admin-Key nötig) ----
$key = clip(@$body['adminKey'],128);
if($key==='' || $key!==$ADMIN_KEY) out(array('ok'=>false,'error'=>'Kein Zugriff'));

if($action==='list'){
  usort($tickets, function($a,$b){ return $b['updated'] - $a['updated']; });
  out(array('ok'=>true,'tickets'=>$tickets));
}
if($action==='reply'){
  $id = clip(@$body['id'],20); $text = clip(@$body['text'],2000);
  foreach($tickets as &$t){ if($t['id']===$id){
    $now = round(microtime(true)*1000);
    $t['msgs'][] = array('from'=>'dev','text'=>$text,'t'=>$now);
    $t['updated']=$now; $t['unreadUser']=1; $t['unreadDev']=0;
    store($DATA_FILE,$tickets); out(array('ok'=>true));
  }}
  out(array('ok'=>false,'error'=>'Ticket nicht gefunden'));
}
if($action==='close'){
  $id = clip(@$body['id'],20);
  foreach($tickets as &$t){ if($t['id']===$id){ $t['status']='geschlossen'; $t['updated']=round(microtime(true)*1000); store($DATA_FILE,$tickets); out(array('ok'=>true)); }}
  out(array('ok'=>false,'error'=>'Ticket nicht gefunden'));
}

out(array('ok'=>false,'error'=>'unbekannte Aktion'));
