# BeaglePTP

Analizzatore, generatore e piattaforma di integrità temporale IEEE 1588/PTP per
BeagleBone Black, gestito da Python tramite dashboard web, API REST, WebSocket
e CLI.

BeaglePTP mantiene il percorso critico di temporizzazione fuori da Python:
Linux, il clock hardware CPSW/CPTS dell'AM335x e `linuxptp` eseguono timestamp,
servo e protocollo PTP; Python gestisce modalità operative, configurazione,
statistiche, allarmi, persistenza, integrità delle sorgenti, report e interfaccia
utente.

> [!IMPORTANT]
> BeaglePTP è una piattaforma avanzata di laboratorio e integrazione, non una
> dichiarazione di accuratezza metrologica o una certificazione militare. Una
> misura assoluta richiede riferimenti tracciabili, calibrazione delle latenze,
> hardware di holdover e una valutazione formale dell'incertezza.

## Indice

- [Funzioni principali](#funzioni-principali)
- [Architettura](#architettura)
- [Modello di integrità temporale](#modello-di-integrità-temporale)
- [Dashboard professionale](#dashboard-professionale)
- [Metriche disponibili](#metriche-disponibili)
- [Modalità operative](#modalità-operative)
- [Installazione di sviluppo](#installazione-di-sviluppo)
- [Installazione sul BeagleBone Black](#installazione-sul-beaglebone-black)
- [Avvio automatico](#avvio-automatico)
- [Spegnimento sicuro dalla dashboard](#spegnimento-sicuro-dalla-dashboard)
- [GPS/GNSS USB e PPS](#gpsgnss-usb-e-pps)
- [Profili PTP](#profili-ptp)
- [API e WebSocket](#api-e-websocket)
- [Persistenza e file di sistema](#persistenza-e-file-di-sistema)
- [Sicurezza](#sicurezza)
- [Limiti hardware e metrologici](#limiti-hardware-e-metrologici)
- [Uso in ambito critico o militare](#uso-in-ambito-critico-o-militare)
- [Migrazione SD → eMMC](#migrazione-sd--emmc)
- [Calibrazione e collaudo](#calibrazione-e-collaudo)
- [Troubleshooting](#troubleshooting)
- [Test](#test)
- [Struttura del progetto](#struttura-del-progetto)
- [Riferimenti](#riferimenti)
- [Licenza](#licenza)

## Funzioni principali

- Timestamp hardware Ethernet tramite CPSW/CPTS e interfaccia Linux PHC.
- Gestione completa del ciclo di vita di `ptp4l` per Analyzer, Slave e
  Grandmaster.
- Gestione di `phc2sys` solamente nelle modalità che devono disciplinare un
  clock.
- Analyzer read-only con `free_running=1`: misura il PTP senza disciplinare PHC
  o `CLOCK_REALTIME`.
- Avvio automatico dell'Analyzer su `eth0` tramite `systemd`.
- Profili predefiniti IEEE 1588, G.8275.1-like, 802.1AS-like e power-profile-like.
- Time Error, path delay, errore di frequenza, stato porta, sequence ID e
  identità del Grandmaster.
- Statistiche media, RMS, deviazione standard, minimo, massimo, picco-picco,
  P50, P95, P99, MTIE e TDEV.
- Path Delay Variation come deviazione standard del mean path delay osservato.
- Soglie warning/critical e allarmi persistenti riconoscibili dall'operatore.
- Database SQLite in modalità WAL con campioni, eventi, allarmi e configurazione.
- Ripristino di campioni, allarmi e policy dopo un riavvio.
- Export CSV e report JSON.
- Dashboard responsiva multi-pagina e aggiornamenti live via WebSocket.
- API senza login sulla sola interfaccia USB `192.168.7.2`; bearer token
  opzionale se si modifica volontariamente l'esposizione di rete.
- Spegnimento sicuro dalla dashboard con conferma digitata e autorizzazione
  Polkit limitata al solo power-off.
- Monitor GPSD per ricevitori GNSS USB auto-rilevati.
- Chrony configurato con sole sorgenti locali GNSS/PPS, senza pool NTP pubblici.
- Stato di integrità `TRUSTED`, `DEGRADED`, `HOLDOVER` o `UNTRUSTED`.
- Allow-list persistente delle identità Grandmaster.
- Rilevamento di cambio GM, GM non autorizzato, time-step, sequence gap, perdita
  GPS/PPS e dati PTP obsoleti.
- Self-test di interfaccia, PHC, timestamp hardware ed eseguibili `linuxptp`.
- Simulatore deterministico per sviluppo senza hardware.
- Unità `systemd` limitata con utente dedicato, capability ristrette, device
  allow-list e protezioni del filesystem/kernel.

## Architettura

```text
                           ┌──────────────────────────────┐
 GNSS USB / NMEA ─────────►│ GPSD 127.0.0.1:2947         │
 PPS (se disponibile) ────►│ SHM 0 = UTC / SHM 1 = PPS   │
                           └──────────────┬───────────────┘
                                          │
                                          ▼
                                  ┌──────────────┐
                                  │    Chrony    │
                                  │ system clock │
                                  └──────────────┘

 Ethernet PTP
      │
      ▼
 CPSW MAC + CPTS ── /dev/ptp0 (PHC) ── Linux timestamping API
      │                                      │
      └──────── PTP event packets ─────── ptp4l
                                             │ log + UDS management
                                             ▼
                                  ┌────────────────────────┐
                                  │ BeaglePTP Python engine │
                                  ├────────────────────────┤
                                  │ source integrity        │
                                  │ alarms / statistics     │
                                  │ SQLite / CSV / JSON     │
                                  │ REST / WebSocket        │
                                  └────────────┬───────────┘
                                               │
                                               ▼
                                      Dashboard professionale
```

Python non genera timestamp PTP e non è nel percorso real-time del servo.

## Modello di integrità temporale

BeaglePTP non considera automaticamente attendibile una sorgente soltanto
perché è raggiungibile. La decisione combina freschezza GNSS, presenza PPS,
campioni PTP e autorizzazione del Grandmaster.

| Stato | Significato |
|---|---|
| `TRUSTED` | Fix GNSS 3D recente, PPS recente, PTP corrente e GM autorizzato |
| `DEGRADED` | È disponibile una sorgente parziale, ad esempio GNSS USB senza PPS o PTP senza policy completa |
| `HOLDOVER` | Le sorgenti live sono state perse, ma non è ancora scaduto l'intervallo di holdover configurato |
| `UNTRUSTED` | Nessuna sorgente valida oppure holdover scaduto |

La policy è fail-closed: con allow-list vuota il traffico PTP può essere
analizzato, ma l'integrità complessiva non diventa `TRUSTED`.

L'incertezza mostrata durante il holdover è una stima conservativa. Non deve
essere interpretata come specifica dell'oscillatore finché il sistema non viene
equipaggiato con un OCXO/rubidio caratterizzato e collaudato in temperatura.

## Dashboard professionale

La dashboard è servita direttamente dal dispositivo:

```text
http://<indirizzo-beaglebone>:8080
```

Nella configurazione USB predefinita:

```text
http://192.168.7.2:8080
```

### Overview

- Time Error live con indicazione in nanosecondi.
- Esito `PASS`, `MARGINAL` o `FAIL` contro le soglie configurate.
- Grafici selezionabili TE, path delay e frequency error.
- Dataset PTP corrente.
- RMS, picco-picco, P99, PDV e frequency error.
- Sintesi MTIE/TDEV.
- Stato hardware, link e allarmi attivi.

### Time Integrity

- Decisione `TRUSTED/DEGRADED/HOLDOVER/UNTRUSTED`.
- Incertezza UTC stimata e motivazioni della decisione.
- Dispositivo GPS, driver, fix, satelliti, HDOP/VDOP e freschezza.
- Presenza PPS, età e offset osservato.
- Stato e riferimento Chrony.
- Grandmaster attivo, allow-list e autorizzazione.
- Età del campione PTP e del riferimento.
- Ultimo timestamp assoluto ricevuto nei messaggi `Sync`/`Follow_Up`.
- Timestamp PTP grezzo, conversione UTC, scala temporale, `currentUtcOffset`,
  tracciabilità, dominio, trasporto e conteggio pacchetti osservati.
- Catena visuale GNSS → PPS → PTP → clock.

Il riquadro **Received PTP time** non mostra l'orologio locale della BeagleBone:
mostra l'ultimo timestamp trasportato sul filo dal master. Il timestamp grezzo è
convertito in UTC solamente quando un messaggio `Announce` dello stesso dominio
dichiara sia la scala PTP sia valido `currentUtcOffset`. In caso contrario resta
visibile il valore PTP grezzo e la conversione UTC viene marcata non valida. Un
segnale su un dominio differente viene contato, ma non viene usato come orario
del profilo configurato.

### Time Error

- Grafico TE con maschera warning/critical.
- Minimo, massimo, RMS, deviazione standard e percentili.
- Export dei campioni grezzi.

### Stability

- MTIE e TDEV per gli intervalli supportati dalla finestra disponibile.
- Grafico e tabella di stabilità.
- Stato `ACQUIRING` finché non sono presenti campioni sufficienti.

### PTP Protocol

- Profilo, domain number, trasporto e delay mechanism.
- Dataset parent/current e risposta `pmc` read-only.
- Stato CPTS, PHC, MAC, link e capacità timestamp.
- Log operativo `linuxptp`.

### Alarms & Events

- Allarmi attivi e storici.
- Severità, messaggio, stato e acknowledgement.
- Eventi di cambio modalità, configurazione e integrità.

### Setup

- Profilo e dominio PTP.
- Timestamp hardware/software e one-step/two-step.
- Analyzer read-only.
- Soglie TE, delay, time-step e stale timeout.
- GPSD on/off.
- Allow-list Grandmaster.
- Durata del holdover.

La configurazione è modificabile solamente con strumento in `IDLE`.

## Metriche disponibili

| Metrica | Descrizione |
|---|---|
| Time Error / offset | Scarto osservato da `ptp4l` rispetto alla sorgente selezionata |
| Mean path delay | Stima del ritardo del percorso PTP |
| PDV proxy | Deviazione standard dei campioni di path delay |
| Frequency error | Stima lineare della pendenza del Time Error, espressa in ppb |
| RMS TE | Radice della media dei quadrati del Time Error |
| Peak-to-peak | Differenza fra massimo e minimo della finestra |
| P50/P95/P99 | Percentili del modulo del Time Error |
| MTIE | Maximum Time Interval Error per intervallo di osservazione |
| TDEV | Time Deviation per intervallo supportato dai campioni |
| PPS offset | Differenza riportata da GPSD tra PPS e clock locale |
| UTC uncertainty | Stima prudenziale costruita dalle sorgenti disponibili |

MTIE e TDEV lunghi richiedono acquisizioni continue sufficientemente estese.
Una finestra breve non può produrre valori validi per `τ` lunghi.

## Modalità operative

### Analyzer read-only

```sh
beagleptp run analyzer --interface eth0 --profile default
```

Configura `ptp4l` come client con `free_running=1` e non avvia `phc2sys`. Il
dispositivo partecipa agli scambi necessari a stimare delay e offset, ma non
disciplina PHC o system clock.

Read-only significa quindi «non corregge i clock locali», non «tap Ethernet
completamente passivo». Un decoder totalmente passivo non può ricavare da un
solo punto di osservazione un offset two-way assoluto senza riferimenti
addizionali.

### Slave

```sh
beagleptp run slave --interface eth0 --profile default
```

`ptp4l` disciplina il PHC e `phc2sys -a -r` disciplina `CLOCK_REALTIME`.

### Grandmaster / generatore

```sh
beagleptp run grandmaster --interface eth0 --profile default
```

Il system clock viene trasferito al PHC e `ptp4l` trasmette Sync, Follow_Up,
Announce e risposte delay. Senza GNSS/PPS o altro riferimento tracciabile è
soltanto un generatore di protocollo/local GM, non un Grandmaster UTC affidabile.

Il preset usa `clockClass 248` per evitare di dichiarare falsamente una sorgente
primaria tracciabile.

### Simulator

```sh
beagleptp run simulator --duration 30 --json
```

Genera rumore di fase, wander ed escursioni controllate senza accedere a
hardware o rete PTP.

## Installazione di sviluppo

Richiede Python 3.11 o successivo.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
beagleptp serve --start simulator
```

Aprire <http://127.0.0.1:8080>.

Smoke test headless:

```sh
beagleptp run simulator --duration 8 --json
```

## Installazione sul BeagleBone Black

Usare un'immagine Debian con:

```text
CONFIG_PTP_1588_CLOCK=y
CONFIG_TI_CPTS=y
CONFIG_PPS=y
```

Installazione:

```sh
cd /home/beagle/ptp-project
sudo ./deploy/install-bbb.sh
```

Lo script:

1. installa `linuxptp`, `ethtool`, Python, GPSD, Chrony, PPS tools e Polkit;
2. crea l'utente di servizio non-login `beagleptp`;
3. installa il virtual environment in `/opt/beagleptp/venv`;
4. vincola la dashboard all'indirizzo USB `192.168.7.2`, senza token;
5. configura permessi PHC e directory persistenti;
6. configura GPSD USB auto-discovery;
7. sostituisce i pool NTP con soli refclock GNSS/PPS locali;
8. salva la configurazione Chrony originale;
9. installa la policy limitata per lo spegnimento sicuro dalla dashboard;
10. abilita GPSD, Chrony e BeaglePTP al boot.

Verifica hardware:

```sh
sudo -u beagleptp /opt/beagleptp/venv/bin/beagleptp doctor \
  --interface eth0 --ptp-device /dev/ptp0
```

## Avvio automatico

L'unità installata avvia dashboard, API, monitor GPS e Analyzer read-only:

```sh
systemctl is-enabled beagleptp
systemctl is-active beagleptp
```

Risultato atteso:

```text
enabled
active
```

Controllo processi:

```sh
pgrep -a ptp4l
pgrep -a phc2sys
```

In Analyzer deve esistere `ptp4l` e non deve esistere `phc2sys`.

Log live:

```sh
sudo journalctl -u beagleptp -f
```

Arresto e riavvio:

```sh
sudo systemctl stop beagleptp
sudo systemctl restart beagleptp
```

## Spegnimento sicuro dalla dashboard

Il pulsante rosso `⏻ SPEGNI` è disponibile nella barra superiore della
dashboard. Serve a terminare la misura, chiudere correttamente il database e
richiedere a Linux un normale power-off prima di togliere l'alimentazione.

Procedura:

1. premere `⏻ SPEGNI`;
2. leggere l'avviso e digitare esattamente `SPEGNI`;
3. premere `Arresta il sistema`;
4. attendere che la dashboard perda la connessione e che tutti i LED della
   BeagleBone siano spenti;
5. solo a quel punto scollegare l'alimentazione.

Lo spegnimento non è un comando shell generico. L'endpoint accetta soltanto un
payload fisso con conferma esatta `SPEGNI` e funziona esclusivamente quando
`BEAGLEPTP_ALLOW_POWEROFF=1`. La dashboard predefinita ascolta solamente
sull'indirizzo USB `192.168.7.2`. La regola Polkit concede all'utente non-login
`beagleptp` solamente le azioni logind `power-off`; non concede `sudo`, reboot o
gestione arbitraria delle unità systemd.

Configurazione installata:

```text
/etc/beagleptp/beagleptp.env
/etc/polkit-1/rules.d/60-beagleptp-poweroff.rules
```

Per disabilitare volontariamente il pulsante:

```sh
sudo sed -i 's/^BEAGLEPTP_ALLOW_POWEROFF=.*/BEAGLEPTP_ALLOW_POWEROFF=0/' \
  /etc/beagleptp/beagleptp.env
sudo systemctl restart beagleptp
```

Il tasto fisico POWER della BeagleBone e `sudo poweroff` da SSH rimangono
alternative valide. Non togliere direttamente la tensione mentre Linux è in
esecuzione, perché SQLite, filesystem e log potrebbero non essere stati ancora
sincronizzati.

## GPS/GNSS USB e PPS

### Percorso UTC grossolano

Un ricevitore supportato da GPSD può comparire come:

```text
/dev/ttyACM0
/dev/ttyUSB0
```

GPSD viene configurato con USB auto-discovery e BeaglePTP usa esclusivamente il
socket locale `127.0.0.1:2947`.

Controlli dopo il collegamento:

```sh
lsusb
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
systemctl status gpsd.socket gpsd chrony beagleptp
chronyc sources -v
chronyc tracking
```

### Sorgenti Chrony

```text
SHM 0 / GNSS = NMEA o time-of-day del ricevitore
SHM 1 / PPS  = impulso di fase, preferito e associato a GNSS
```

Non sono configurati `pool`, `server` o `peer` Internet. Senza ricevitore,
Chrony rimane `Not synchronised` e BeaglePTP rimane `UNTRUSTED`.

### Precisione USB

USB/NMEA può stabilire UTC e correggere errori di secondi o millisecondi, ma non
è un riferimento di fase sub-microsecondo. Anche un PPS trasportato attraverso
un convertitore USB-seriale risente del polling e del jitter USB.

Per precisione superiore usare un ricevitore timing con:

- USB per dati NMEA/UBX e numero del secondo;
- uscita fisica 1 PPS separata;
- livello elettrico e fronte attivo documentati;
- accuratezza PPS dichiarata;
- antenna e stato di fix controllabili.

Il kernel installato supporta LinuxPPS e `pps-gpio`, ma pin e overlay dipendono
dal ricevitore e dalla revisione della scheda. Non collegare un PPS sconosciuto
direttamente all'header e non collegare mai livelli RS-232 a un GPIO.

Quando `/dev/pps0` è stato configurato:

```sh
sudo ppstest /dev/pps0
chronyc sources -v
chronyc tracking
```

Per ulteriori dettagli vedere [docs/GPS-USB.md](docs/GPS-USB.md).

## Profili PTP

| Chiave | Uso previsto | Trasporto | Delay |
|---|---|---|---|
| `default` | IEEE 1588 laboratorio | UDPv4 | E2E |
| `g8275.1` | Telecom full timing support-like | L2 | P2P |
| `gptp` | IEEE 802.1AS-like | L2 | P2P |
| `power` | Power profile-like | L2 | P2P |

Questi sono preset di laboratorio. La conformità formale richiede lo standard
applicabile, una topologia conforme, BMCA corretto, parametri completi e test di
interoperabilità/certificazione.

Generazione e audit della configurazione:

```sh
beagleptp generate-config analyzer --profile default
beagleptp generate-config grandmaster --profile g8275.1 > /tmp/ptp4l.conf
```

## API e WebSocket

OpenAPI interattiva:

```text
http://<host>:8080/api/docs
```

Endpoint principali:

| Metodo | Endpoint | Funzione |
|---|---|---|
| `GET` | `/api/status` | Stato completo dello strumento |
| `GET` | `/api/integrity` | Decisione di integrità temporale |
| `GET` | `/api/gps` | Stato GPSD, fix, satelliti e PPS |
| `GET` | `/api/ptp-time` | Timestamp PTP ricevuto, UTC e proprietà della scala temporale |
| `GET` | `/api/doctor` | Self-test hardware/software |
| `GET` | `/api/profiles` | Profili disponibili |
| `GET` | `/api/samples?limit=N` | Campioni recenti |
| `GET` | `/api/report?limit=N` | Report statistico JSON |
| `GET` | `/api/export.csv?limit=N` | Esportazione CSV |
| `PUT` | `/api/config` | Configurazione persistente, solo IDLE |
| `POST` | `/api/start` | Avvio modalità |
| `POST` | `/api/stop` | Arresto misura |
| `POST` | `/api/system/poweroff` | Spegnimento sicuro con conferma `SPEGNI` obbligatoria |
| `POST` | `/api/alarms/{code}/acknowledge` | Acknowledge allarme |
| `GET` | `/api/pmc/{management_id}` | Query PMC allow-listed |
| `WS` | `/api/live` | Sample, allarmi, log, eventi e stato live |

Nell'installazione USB predefinita non viene richiesto un token. Il servizio è
vincolato a:

```text
http://192.168.7.2:8080
```

Un bearer token resta disponibile come protezione opzionale se l'operatore
modifica consapevolmente il servizio per esporlo su un'altra rete. Con token
configurato, REST richiede:

```http
Authorization: Bearer <token>
```

Il WebSocket autentica l'handshake mediante il sottoprotocollo `beagleptp` quando
il token opzionale è configurato, evitando di inserire credenziali nell'URL e
nei normali access log. L'API non accetta comandi shell o argomenti arbitrari.

Per abilitare volontariamente un token:

```sh
sudo sed -i \
  "s/^BEAGLEPTP_API_TOKEN=.*/BEAGLEPTP_API_TOKEN=$(openssl rand -hex 32)/" \
  /etc/beagleptp/beagleptp.env
sudo systemctl restart beagleptp
```

## Persistenza e file di sistema

| Percorso | Contenuto |
|---|---|
| `/home/beagle/ptp-project` | Sorgenti modificabili |
| `/opt/beagleptp/venv` | Installazione Python eseguita dal servizio |
| `/var/lib/beagleptp/beagleptp.sqlite3` | Campioni, eventi, allarmi e settings |
| `/run/beagleptp` | Socket UDS e configurazione runtime `ptp4l` |
| `/etc/beagleptp/beagleptp.env` | Ambiente servizio e token opzionale |
| `/etc/systemd/system/beagleptp.service` | Unità di boot |
| `/etc/polkit-1/rules.d/60-beagleptp-poweroff.rules` | Autorizzazione limitata al power-off |
| `/etc/default/gpsd` | Auto-discovery GPSD |
| `/etc/chrony/chrony.conf` | Policy GNSS/PPS locale |
| `/etc/chrony/chrony.conf.pre-beagleptp` | Backup configurazione Chrony Debian |

SQLite usa WAL e conserva dati e allarmi fra i riavvii. La configurazione
applicata dalla dashboard è salvata nella tabella `settings`.

## CLI

```text
beagleptp doctor [--interface eth0] [--ptp-device /dev/ptp0]
beagleptp generate-config {analyzer,slave,grandmaster} [--profile PROFILE]
beagleptp run {analyzer,slave,grandmaster,simulator} [--duration SEC] [--json]
beagleptp serve [--host IP] [--port 8080] [--start MODE] [--api-token TOKEN]
```

## Sicurezza

L'unità `systemd` include:

- utente/gruppo dedicato `beagleptp`;
- `NoNewPrivileges=true`;
- capability bounding set ristretto;
- accesso device limitato a `/dev/ptp0`;
- `ProtectSystem=strict`;
- home, kernel tunables, moduli, control groups e kernel logs protetti;
- namespace e famiglie di socket limitati;
- directory scrivibili limitate a runtime e database;
- umask restrittiva;
- restart controllato e limite ai tentativi.
- dashboard vincolata all'indirizzo USB `192.168.7.2` nell'unità predefinita;
- spegnimento remoto disabilitato se `BEAGLEPTP_ALLOW_POWEROFF` non vale `1`;
- policy Polkit ristretta alle sole azioni logind di power-off per l'utente
  `beagleptp`.

Le capability `CAP_NET_ADMIN`, `CAP_NET_RAW`, `CAP_SYS_TIME` e `CAP_SYS_NICE`
sono necessarie alle modalità PTP reali. Ridurle ulteriormente richiederebbe
unità separate per Analyzer e Grandmaster.

### Azioni ancora necessarie prima di una rete non fidata

- usare chiavi SSH e disabilitare login con password;
- eliminare password salvate in chiaro da SSH FS;
- cambiare le credenziali predefinite;
- aggiungere HTTPS/mTLS o un reverse proxy autenticato;
- separare management e rete PTP;
- configurare firewall e allow-list di management;
- implementare Authentication TLV PTP e gestione delle chiavi compatibile con
  tutti i nodi;
- aggiungere aggiornamenti firmati, secure/measured boot e recovery;
- inviare audit e log a un sistema remoto protetto.

Il collegamento USB senza token presuppone accesso fisico controllato al MacBook
e alla BeagleBone. Se la dashboard viene esposta su Ethernet o Wi-Fi, abilitare
almeno il bearer token; un token su HTTP protegge dall'accesso casuale ma non
cifra il traffico e non è sufficiente su una rete ostile.

## Limiti hardware e metrologici

- Il CPTS AM335x effettua timestamp nel percorso MAC, non al connettore RJ45.
- Le latenze fisse RX/TX e l'asimmetria del PHY devono essere misurate e
  compensate.
- Il BeagleBone Black classico non offre una PPS PTP programmabile pronta sul
  connettore.
- Il percorso dispone di un solo MAC Ethernet operativo per questa funzione e
  non realizza una piattaforma dual-port boundary/transparent-clock completa.
- L'oscillatore standard non è un holdover qualificato.
- Alimentazione, temperatura, EMI e carico CPU influenzano il risultato.
- USB/NMEA non sostituisce un PPS catturato in hardware.
- MTIE/TDEV calcolati dal software non costituiscono da soli una calibrazione.
- Non sono presenti ingressi/uscite 10 MHz calibrati, PCAP decoder o doppio
  canale indipendente.

## Uso in ambito critico o militare

Il sistema corrente è adatto come:

- prototipo;
- banco di laboratorio;
- strumento didattico/addestrativo;
- analizzatore di integrazione;
- generatore PTP su rete isolata;
- base software per un prodotto più robusto.

Non deve essere dichiarato apparato militare, safety-critical o mission-critical
senza almeno:

- requisiti di accuratezza, disponibilità, detection e recovery formalizzati;
- due sorgenti PNT indipendenti;
- GNSS timing con anti-jam/anti-spoof appropriato al programma;
- PPS/10 MHz e oscillatore di holdover caratterizzato;
- timestamp hardware al punto di riferimento calibrato;
- alimentazione, contenitore e componenti industriali/rugged;
- secure boot, gestione chiavi, audit e supply-chain assurance;
- prove ambientali ed EMC selezionate per la piattaforma;
- calibrazione tracciabile e budget d'incertezza;
- validazione da parte dell'autorità competente.

MIL-STD-810 richiede environmental tailoring per il ciclo di vita reale; non è
una singola prova universale. MIL-STD-461 riguarda emissioni e suscettibilità
EMI degli apparati. Per veicoli terrestri a 28 V possono essere applicabili i
requisiti MIL-STD-1275. La selezione finale appartiene al programma e
all'autorità di qualifica.

## Migrazione SD → eMMC

La migrazione è possibile solamente dopo aver identificato senza ambiguità SD
ed eMMC.

Sul sistema verificato:

```text
root corrente: /dev/mmcblk0p3
mmcblk0: microSD da circa 32 GB
dati usati: circa 1,6 GB
eMMC prevista: circa 4 GB
```

Non usare un clone raw `dd` da 32 GB verso 4 GB. Usare il flasher filesystem-
aware BeagleBoard dopo cold boot forzato dalla SD con pulsante S2/BOOT.

Procedura completa, safety gate e recovery:

[docs/EMMC-MIGRATION.md](docs/EMMC-MIGRATION.md)

## Calibrazione e collaudo

Prima di trattare i risultati come misure:

1. Usare una rete dedicata o isolata.
2. Disabilitare Energy Efficient Ethernet sul percorso temporale.
3. Usare link partner e switch PTP documentati.
4. Confrontare almeno due riferimenti indipendenti e uno calibrato.
5. Misurare latenza ingresso/uscita, PHY, cavo e asimmetria.
6. Ripetere le prove su temperatura e alimentazione.
7. Eseguire acquisizioni di 12–24 ore per MTIE/TDEV significativi.
8. Provare perdita pacchetti, riordino, variazioni delay e BMCA.
9. Provare perdita GNSS, ingresso/uscita holdover e recovery.
10. Verificare che nessun altro servo controlli lo stesso clock.
11. Conservare configurazione, firmware, raw data e riferimenti usati.
12. Dichiarare un budget d'incertezza, non soltanto il valore medio osservato.

## Troubleshooting

### Dashboard non raggiungibile

```sh
systemctl status beagleptp
sudo journalctl -u beagleptp -b --no-pager
ss -ltn | grep ':8080'
```

### La dashboard richiede ancora il vecchio token

```sh
sudo sed -i 's/^BEAGLEPTP_API_TOKEN=.*/BEAGLEPTP_API_TOKEN=/' \
  /etc/beagleptp/beagleptp.env
sudo systemctl restart beagleptp
```

Ricaricare la pagina. Quando `/api/status` comunica che l'autenticazione è
disattivata, la dashboard elimina automaticamente il token precedentemente
salvato dal browser.

### Pulsante SPEGNI disabilitato o spegnimento rifiutato

```sh
sudo grep '^BEAGLEPTP_ALLOW_POWEROFF=' \
  /etc/beagleptp/beagleptp.env
test -r /etc/polkit-1/rules.d/60-beagleptp-poweroff.rules && echo policy-present
sudo journalctl -u beagleptp -b --no-pager
```

Deve essere presente:

```text
BEAGLEPTP_ALLOW_POWEROFF=1
```

Se la policy è assente, rieseguire `sudo ./deploy/install-bbb.sh`. Non sostituire
la policy con un'autorizzazione `sudo` generale per l'utente del servizio.

### Analyzer attivo ma nessun campione

```sh
ip link show eth0
ethtool eth0
ethtool -T eth0
pgrep -a ptp4l
sudo journalctl -u beagleptp -f
```

Controllare carrier, dominio, profilo, E2E/P2P, trasporto L2/UDP e presenza di
un Grandmaster compatibile.

### GPSD online ma nessun ricevitore

```sh
lsusb
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
systemctl status gpsd.socket gpsd
ss -tn | grep ':2947'
```

Se il device non compare, verificare cavo dati, alimentazione, hub USB e driver
kernel.

### Chrony non sincronizzato

```sh
chronyc sources -v
chronyc tracking
```

`#? GNSS` e `#? PPS` sono normali senza fix o PPS. Non aggiungere un pool
Internet soltanto per eliminare l'allarme: prima verificare la sorgente locale.

### PPS assente

```sh
ls -l /dev/pps*
sudo ppstest /dev/pps0
```

Se `/dev/pps0` non esiste, il ricevitore potrebbe non esportare PPS, il driver
USB potrebbe non trasportare DCD oppure manca l'overlay/pinmux GPIO.

### Configurazione non modificabile

Premere **Stop** nella dashboard. La `PUT /api/config` viene rifiutata durante
una misura attiva per evitare cambiamenti non controllati.

### Servizio lento a fermarsi

Le connessioni WebSocket vengono chiuse esplicitamente e l'unità usa
`TimeoutStopSec=10`. Controllare client bloccati e log se il limite viene
raggiunto.

## Test

```sh
python -m pytest -q
python -m ruff check .
beagleptp run simulator --duration 3 --json
```

I test automatici non richiedono hardware PTP. Il collaudo finale deve essere
ripetuto sulla board perché kernel, device tree, PHY e immagine determinano le
capacità effettive.

Ultima verifica della release documentata:

```text
15 test superati
Ruff senza errori
JavaScript dashboard sintatticamente valido
gpsd.socket, gpsd, chrony e beagleptp attivi
Analyzer avviato automaticamente
phc2sys assente in Analyzer read-only
```

## Struttura del progetto

```text
ptp-project/
├── configs/                  note e configurazioni
├── deploy/
│   ├── beagleptp.service     unità systemd
│   ├── chrony-gps.conf       policy GNSS/PPS senza NTP pubblico
│   ├── gpsd.default          auto-discovery USB
│   ├── install-bbb.sh        installer idempotente
│   └── 99-beagleptp.rules    permessi PHC
├── docs/
│   ├── EMMC-MIGRATION.md     migrazione e recovery storage
│   └── GPS-USB.md            GNSS, PPS e limiti di accuratezza
├── src/beagleptp/
│   ├── api.py                FastAPI, REST e WebSocket
│   ├── cli.py                interfaccia a riga di comando
│   ├── engine.py             stato, allarmi e integrità
│   ├── gps.py                client JSON GPSD
│   ├── hardware.py           probe PHC/interfaccia
│   ├── linuxptp.py           ptp4l/phc2sys/pmc
│   ├── models.py             modelli e profili
│   ├── parsers.py            parser log/dataset
│   ├── statistics.py         TE, MTIE, TDEV e PDV
│   ├── store.py              SQLite/WAL
│   └── web/index.html        dashboard incorporata
├── tests/                    unit e API test
├── LICENSE                   Apache License 2.0
├── README.md
└── pyproject.toml
```

## Roadmap

- Authentication TLV IEEE 1588 e gestione sicura delle chiavi.
- HTTPS/mTLS e ruoli viewer/operator/admin.
- Audit append-only firmato e log remoto.
- Acquisizione PCAP e analisi messaggi PTP.
- Statistiche perdita, duplicati, reorder e BMCA flap più estese.
- Ingresso PPS hardware board-specific e misura dell'incertezza.
- Supporto 10 MHz, OCXO/rubidio e holdover caratterizzato.
- Calibrazione RX/TX e compensazione asimmetria.
- Hardware dual-port o timing cape/FPGA dedicato.
- Secure boot, aggiornamento firmato e immagine A/B di recovery.

## Riferimenti

- [TI CPSW/CPTS driver guide](https://software-dl.ti.com/processor-sdk-linux/esd/AM335X/11_02_05_02/exports/docs/linux/Foundational_Components/Kernel/Kernel_Drivers/Network/CPSW.html)
- [linuxptp: ptp4l](https://www.linuxptp.org/documentation/ptp4l/)
- [linuxptp: phc2sys](https://www.linuxptp.org/documentation/phc2sys/)
- [linuxptp: pmc](https://www.linuxptp.org/documentation/pmc/)
- [IEEE 1588 Working Group public documents](https://sagroups.ieee.org/1588/public-documents/)
- [GPSD Time Service HOWTO](https://gpsd.io/gpsd-time-service-howto.html)
- [Linux kernel PPS documentation](https://docs.kernel.org/driver-api/pps.html)
- [Chrony documentation](https://chrony-project.org/documentation.html)
- [NISTIR 8323 Rev. 1 — Foundational PNT Profile](https://doi.org/10.6028/NIST.IR.8323r1)
- [BeagleBone Black documentation](https://docs.beagleboard.org/boards/beaglebone/black/)
- [MIL-STD documents — DLA ASSIST Quick Search](https://quicksearch.dla.mil/)

## Licenza

Copyright dei rispettivi contributori. Distribuito secondo
[Apache License 2.0](LICENSE).
