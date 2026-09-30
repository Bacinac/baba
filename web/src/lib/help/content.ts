// The concept guide — hand-authored articles on BABA's mental model, the part
// the code cannot express. Reference material (threshold ranges, class lists,
// event kinds) is generated elsewhere from the catalogues that already own it;
// this file is only the "why".
//
// Bilingual: each article carries an hr and en body. Adding one is a single
// entry here — the /help index and the article route pick it up automatically,
// and a Hint anywhere in the app can deep-link to it by slug.

import type { Locale } from "$lib/kit";

export interface HelpArticle {
  slug: string;
  /** Section grouping on the index, by i18n key. */
  group: "concepts" | "operating";
  title: Record<Locale, string>;
  /** One-line summary for the index card. */
  summary: Record<Locale, string>;
  body: Record<Locale, string>;
}

const KAKO_BABA_VIDI: HelpArticle = {
  slug: "kako-baba-vidi",
  group: "concepts",
  title: { hr: "Kako BABA vidi", en: "How BABA sees" },
  summary: {
    hr: "Put od slike kamere do zapisa u Aktivnosti — i zašto BABA ne koristi detekciju pokreta.",
    en: "The path from camera image to an Activity entry — and why BABA does not use motion detection.",
  },
  body: {
    hr: `
BABA nije klasičan snimač. Klasičan NVR snima piksele i čeka da ih čovjek pregleda; BABA **razumije scenu u stvarnom vremenu** — vidi tko je došao, prati ga dok je prisutan, prepozna ga kad se vrati, i zapisuje to kao događaje, ne kao sate videa.

Sve što vidiš u aplikaciji izlazi iz jednog toka. Vrijedi ga razumjeti jer objašnjava zašto je nešto zapisano, a nešto nije.

## Tok obrade

1. **Kamera → dekodiranje.** Svaka kamera daje RTSP tok koji BABA dekodira jednom i tu jednu sliku dijeli svemu nizvodno.
2. **Detektor.** Na svakom kadru transformer-model traži objekte — osobu, vozilo, životinju — i vraća okvir s pouzdanošću. Ovo je *po kadru*: detektor ne zna da je osoba na ovom kadru ista kao na prošlom.
3. **Tracker.** Povezuje detekcije kroz kadrove u **trackove**: jedna osoba koja se kreće scenom = jedan track, čak i kad je detektor na tren izgubi. Ovdje se rađa pojam „prisustva".
4. **Prepoznavanje (re-ID).** Za svaki track BABA računa vektor izgleda tijela (i lica, ako ga vidi). Time povezuje isti subjekt kroz vrijeme i kamere — ista osoba prepoznata kad se vrati, i sutra, u drugoj jakni.
5. **Event-manager.** Odlučuje što je vrijedno zapisati: ulazak u zonu, zadržavanje, dolazak vozila, kraj posjeta. To puni **Aktivnost**.
6. **Snimanje.** Teče kontinuirano 24/7 neovisno o detekciji; koliko se te snimke zadrži određuje politika čuvanja (vidi [Snimanje](/help/snimanje)). Aktivnost je sloj razumijevanja iznad nje.

## Detekcija nije track

Ovo je razlika koju vrijedi zapamtiti. **Detekcija** je ono što model vidi na jednom kadru — sirovo, može treperiti, može se prevariti (klupa pročitana kao osoba). **Track** je ono što tracker sastavi kroz vrijeme, i tek to postaje prisustvo koje se prati i zapisuje.

Zbog toga Zone editor pokazuje sirove detekcije (da vidiš što model kaže dok kalibriraš), a Aktivnost pokazuje posjete (ono što je preživjelo). Kad se to dvoje razlikuje, ne znači kvar — znači da je pipeline nešto odbacio, i BABA ti to i sivi u pregledu.

## Zašto nema detekcije pokreta

Većina NVR-ova pali analizu tek kad se nešto pomakne. To ima jednu fatalnu manu: **gubi osobu koja stoji**. Tko se umiri — sjedne, čeka, promatra — za takav sustav prestaje postojati.

BABA namjerno ne radi tako. Detektor gleda svaki kadar bez obzira na kretanje, a tracker drži osobu koja se umiri preko izgleda, ne preko pomaka. Pouzdano vidjeti nepomičnu osobu je temeljni zahtjev, ne dodatak — i cijeli niz odluka u sustavu postoji da se ta osoba nikad ne izgubi.

---

Dalje: kako prisustvo postaje zapis u [Aktivnost i posjeti](/help/aktivnost-i-posjeti); kako se odlučuje što je gdje bitno u [Zone](/help/zone); kako BABA zna tko je tko u [Identiteti i prepoznavanje](/help/identiteti).
`,
    en: `
BABA is not an ordinary recorder. A classic NVR stores pixels and waits for a human to review them; BABA **understands the scene in real time** — it sees who arrived, follows them while they are present, recognises them when they return, and records that as events rather than hours of video.

Everything you see in the app comes out of one pipeline. It is worth understanding because it explains why something was recorded and something else was not.

## The pipeline

1. **Camera → decode.** Each camera provides an RTSP stream that BABA decodes once and shares that single frame with everything downstream.
2. **Detector.** On every frame a transformer model looks for objects — a person, a vehicle, an animal — and returns a box with a confidence. This is *per frame*: the detector does not know the person on this frame is the same as on the last.
3. **Tracker.** Links detections across frames into **tracks**: one person moving through the scene is one track, even when the detector briefly loses them. This is where "presence" is born.
4. **Recognition (re-ID).** For each track BABA computes an appearance vector of the body (and the face, if it sees one). That links the same subject across time and cameras — recognised when they return, and tomorrow, in a different jacket.
5. **Event manager.** Decides what is worth recording: entering a zone, lingering, a vehicle arriving, the end of a visit. This fills **Activity**.
6. **Recording.** Runs continuously 24/7, independent of detection; how much of it is kept is governed by the retention policy (see [Recording](/help/snimanje)). Activity is a layer of understanding on top of it.

## A detection is not a track

This is the distinction to remember. A **detection** is what the model sees on a single frame — raw, it can flicker, it can be fooled (a bench read as a person). A **track** is what the tracker assembles over time, and only that becomes a presence that is followed and recorded.

That is why the Zone editor shows raw detections (so you see what the model says while calibrating) and Activity shows visits (what survived). When the two differ it is not a fault — it means the pipeline discarded something, and BABA greys it out in the preview to show you.

## Why there is no motion detection

Most NVRs only start analysing when something moves. That has one fatal flaw: **it loses a person who stands still**. Someone who settles — sits, waits, watches — stops existing for such a system.

BABA deliberately does not work that way. The detector looks at every frame regardless of movement, and the tracker holds a person who settles by their appearance, not their motion. Reliably seeing a stationary person is a core requirement, not an add-on — and a whole chain of decisions in the system exists so that person is never lost.

---

Next: how presence becomes a record in [Activity and visits](/help/aktivnost-i-posjeti); how it is decided what matters where in [Zones](/help/zone); how BABA knows who is who in [Identities and recognition](/help/identiteti).
`,
  },
};

const AKTIVNOST_I_POSJETI: HelpArticle = {
  slug: "aktivnost-i-posjeti",
  group: "concepts",
  title: { hr: "Aktivnost i posjeti", en: "Activity and visits" },
  summary: {
    hr: "Što je posjet, zašto vozilo daje dolazak i odlazak, a osoba jedan neprekinut zapis.",
    en: "What a visit is, why a vehicle yields an arrival and a departure, and a person one continuous record.",
  },
  body: {
    hr: `
Aktivnost je dnevnik onoga što se dogodilo, ne popis snimki. Jedan redak = **jedan posjet**: neprekinuto prisustvo jednog subjekta na jednoj kameri. Ako tracker izgubi pa ponovno uhvati istu osobu, ili se identitet fragmentira kroz nekoliko trackova, BABA ih spaja u jedan posjet — ne dobivaš pet redaka za jedan dolazak.

## Vozilo: dolazak i odlazak su dva događaja

Auto koji parkira ujutro i ode navečer nisu jedan „posjet od 4 sata" — to su dvije stvari koje su se dogodile s razmakom od nekoliko sati. BABA ih zato prikazuje kao **dva reda, svaki na svom vremenu**: dolazak u 04:52, odlazak u 09:11. Tako odlazak iskoči kao svjež događaj kad se dogodi, umjesto da bude zakopan na retku od jutros.

Uz odlazak stoji i oznaka **koliko je bilo parkirano**, a klip svakog reda je kratak bookend: dolazak i parkiranje (~40 s), pa kretanje i odlazak (~40 s). Mrtva sredina — sati parkiranog auta koji stoji — nije ni red ni video.

## Osoba: jedan neprekinut zapis

Osoba je suprotan slučaj. Njezina mirna faza **nije mrtvo vrijeme** — to je prisustvo, i to je cijela poanta. Zato osoba, koliko god se umirila, ostaje **jedan neprekinut zapis i jedan klip**, od dolaska do odlaska. Podijeliti taj posjet na više zapisa značilo bi ponoviti propust koji BABA postoji da izbjegne.

Rez na dolazak/odlazak vrijedi dakle samo za vozila: parkirani auto je stvarno inertan, umireni čovjek nije.

## Trajanje u zoni

Kartica pokazuje kroz koje je zone subjekt prošao i koliko se u svakoj zadržao. To je **zbroj stvarnih intervala** ulaz→izlaz, ne raspon od prvog ulaza do zadnjeg izlaza — inače bi auto koji uđe kroz vrata, parkira, i na izlasku opet okrzne vrata pokazivao „u vratima 4 sata".

Iznimka je **parkirna zona**: parkirani auto ondje ne emitira evente dok stoji, pa se za takvu zonu prikazuje koliko je bio parkiran (trajanje posjeta), ne titravi zbroj.

## Aktivnost vs. snimka

Snimanje teče kontinuirano — Aktivnost je sloj iznad njega. Klik na redak reže klip iz kontinuirane snimke za taj trenutak. Gusta traka na vremenskoj crti ne znači „poplava detekcija" nego samo da snimka ondje postoji; što je BABA *razumjela* vidiš u redovima, ne u traci.

---

Vidi i: [Zone](/help/zone), [Snimanje i pohrana](/help/snimanje).
`,
    en: `
Activity is a log of what happened, not a list of recordings. One row = **one visit**: a continuous presence of one subject on one camera. If the tracker loses and reacquires the same person, or the identity fragments across several tracks, BABA stitches them into one visit — you don't get five rows for one arrival.

## A vehicle: arrival and departure are two events

A car that parks in the morning and leaves in the evening is not one "4-hour visit" — it is two things that happened hours apart. So BABA shows them as **two rows, each at its own time**: an arrival at 04:52, a departure at 09:11. The departure surfaces fresh when it happens, instead of being buried on a row from the morning.

The departure carries a tag for **how long it was parked**, and each row's clip is a short bookend: arrival and parking (~40 s), then start-up and drive-off (~40 s). The dead middle — hours of a parked car sitting still — is neither a row nor a clip.

## A person: one continuous record

A person is the opposite case. Their still phase is **not dead time** — it is presence, and that is the whole point. So a person, however still they go, stays **one continuous record and one clip**, from arrival to departure. Splitting it would repeat the very failure BABA exists to avoid.

The arrival/departure split therefore applies to vehicles only: a parked car is genuinely inert, a settled person is not.

## Time in a zone

The card shows which zones a subject passed through and how long it spent in each. That is the **sum of actual enter→exit intervals**, not the span from first enter to last exit — otherwise a car that drives in through a gate, parks, and clips the gate again on the way out would read "in the gate for 4 hours".

The exception is a **parking zone**: a parked car emits no events while it sits, so for that kind BABA shows how long it was parked (the visit length), not the flickering sum.

## Activity vs. recording

Recording runs continuously — Activity is a layer above it. Clicking a row cuts a clip from the continuous recording for that moment. A dense band on the timeline does not mean "a flood of detections", only that footage exists there; what BABA *understood* is in the rows, not the band.

---

See also: [Zones](/help/zone), [Recording and storage](/help/snimanje).
`,
  },
};

const ZONE: HelpArticle = {
  slug: "zone",
  group: "concepts",
  title: { hr: "Zone", en: "Zones" },
  summary: {
    hr: "Kako crtaš gdje ti je stalo, koje vrste zona postoje i zašto se sve mjeri po stopalu.",
    en: "How you draw where you care, the zone kinds, and why everything is measured by the foot.",
  },
  body: {
    hr: `
Zona je poligon koji crtaš na slici kamere da kažeš „ovdje mi je stalo". Sve zone rade po istom sidru: **stopalo subjekta** — donji centar okvira, mjesto gdje objekt dodiruje tlo. Poligon nacrtan jednom ponaša se isto svugdje gdje se procjenjuje, jer se uvijek pita je li *stopalo* unutra, ne cijela kutija.

## Vrste zona

- **Zona interesa / ulaza** — ovdje želiš događaje: netko ušao, netko se zadržao. Ovo je najčešća zona.
- **Parkirna zona** — mjesto gdje vozila legitimno stoje danima. Ondje se parkirani auto ne potiskuje kao smeće (vidi [Ugađanje](/help/ugadjanje)), a trajanje se mjeri kao parkirano vrijeme.
- **Zabranjena zona (restricted)** — mjesto gdje ništa ne bi smjelo biti. Za razliku od zone interesa, ova okida i na **nepomične** objekte: nešto ostavljeno ondje i dalje je događaj.
- **Ignore zona** — mrtvo tlo. Susjedova terasa, javni pločnik. Ovdje BABA ne prati **ništa** — detekcije se odbacuju na ulazu u tracker, pa ne troše ni prepoznavanje ni snimku eventa.

Ignore zona nije rješenje za statično smeće (to rješava samoučeći registar fantoma) — ona je za mjesta gdje se *stvarni* subjekti pojavljuju, a ti ih jednostavno ne želiš.

## Pravila po klasi

Svaka zona može imati pravila po klasi: minimalna pouzdanost, minimalna veličina okvira, minimalno zadržavanje prije nego se javi event, i cooldown da isti prolaz ne okida dvaput. Time gasiš npr. auto na cesti u kutu kadra, a zadržavaš ljude na ulazu.

## Motion gate — i zašto osoba prolazi

Parkirna, ulazna i interesna zona po zadanom **ne okidaju na nepomične objekte**: podrhtavanje okvira parkiranog auta preko ruba poligona inače pravi beskonačan niz ulaz/izlaz. Ali za **osobu je to isključeno**: netko tko se zaustavi u zoni interesa ne miruje slučajno — to je zadržavanje, najizvještajnija stvar koju kamera može vidjeti. Vozila zadržavaju gate, ljudi ne.

## Pregled sirovog feeda

Zone editor namjerno prikazuje **sirove detekcije** (izlaz detektora, prije trackera) da vidiš što model govori dok crtaš i kalibriraš. Ono što pipeline već odbacuje — statični fantom, detekcija u ignore zoni — prikazano je sivo i iscrtkano, s razlogom. Sivo znači „ovo ne ide dalje", ne kvar.

---

Povezano: [Kako BABA vidi](/help/kako-baba-vidi), [Ugađanje i fantomi](/help/ugadjanje).
`,
    en: `
A zone is a polygon you draw on the camera image to say "I care about this". Every zone works from the same anchor: the subject's **foot** — the bottom-centre of the box, where the object meets the ground. A polygon drawn once behaves the same everywhere it is evaluated, because it always asks whether the *foot* is inside, not the whole box.

## Zone kinds

- **Interest / entry zone** — you want events here: someone entered, someone lingered. This is the most common zone.
- **Parking zone** — a place where vehicles legitimately sit for days. A parked car there is not suppressed as clutter (see [Tuning](/help/ugadjanje)), and its time is measured as parked duration.
- **Restricted zone** — a place where nothing should be. Unlike an interest zone, it fires even on **stationary** objects: something left there is still an event.
- **Ignore zone** — dead ground. A neighbour's terrace, a public pavement. BABA tracks **nothing** here — detections are dropped at the entrance to the tracker, so they cost neither recognition nor a clip.

An ignore zone is not the answer to static clutter (the self-learning phantom registry handles that) — it is for ground where *real* subjects appear and you simply don't want them.

## Per-class rules

Each zone can carry rules per class: minimum confidence, minimum box size, minimum dwell before an event fires, and a cooldown so one pass doesn't fire twice. With these you silence, say, a car on the road in the corner of the frame while keeping people at the entrance.

## The motion gate — and why a person passes

Parking, entry and interest zones by default **do not fire on stationary objects**: a parked car's box jitter across a polygon edge would otherwise produce an endless enter/exit stream. But for a **person this is disabled**: someone who stops inside an interest zone is not still by accident — that is loitering, the single most report-worthy thing a camera can see. Vehicles keep the gate, people don't.

## The raw-feed preview

The Zone editor deliberately shows **raw detections** (the detector's output, before the tracker) so you see what the model says while drawing and calibrating. What the pipeline already discards — a static phantom, a detection in an ignore zone — is drawn greyed and dashed, with a reason. Grey means "this goes no further", not a fault.

---

Related: [How BABA sees](/help/kako-baba-vidi), [Tuning and phantoms](/help/ugadjanje).
`,
  },
};

const IDENTITETI: HelpArticle = {
  slug: "identiteti",
  group: "concepts",
  title: { hr: "Identiteti i prepoznavanje", en: "Identities and recognition" },
  summary: {
    hr: "Kako BABA prepoznaje istu osobu kad se vrati — lice, tijelo, i zašto je lice glavni signal.",
    en: "How BABA recognises the same person when they return — face, body, and why the face is the strong signal.",
  },
  body: {
    hr: `
Prepoznavanje je ono što BABA-u razlikuje od snimača: ista osoba dobije isti identitet kad se vrati — sat kasnije, sutra, u drugoj odjeći. To radi preko **vektora izgleda** (embeddinga): brojčani otisak koji je blizu za istu osobu, a dalek za druge.

## Lice i tijelo — dva signala

- **Lice** je jaki signal. Kad BABA vidi lice dovoljno jasno, ono odlučuje: povezuje trackove kroz sesije i imenuje osobu.
- **Tijelo** je slabiji, dopunski signal. Prepoznaje istu osobu preko cijele pojave (OSNet body re-ID) i kad lice nije vidljivo — s kapuljačom, s leđa, u mraku.

Ključno pravilo: **tijelo samo za sebe nikad ne smije doseći imenovani identitet.** Previše je pogrešaka moguće (dvoje ljudi u sličnoj jakni). Tijelo smije imenovati osobu tek kad se nadoveže na track koji je lice već potvrdilo — to je *face-anchored body chain*. Tako se ista osoba prepozna i na kadrovima gdje joj se lice ne vidi, ali bez rizika da slična pojava dobije tuđe ime.

## Imenovanje i referentne fotografije

Osobu imenuješ tako da joj priložiš referentne fotografije. Što više raznolikih, imenovanih fotki po osobi, to pouzdanije prepoznavanje. BABA ih uvozi iz **OPUS · Libraryja** (obiteljska fotobiblioteka; osoba iz biblioteke postaje identitet jednim potezom) ili iz **Immicha** — u oba slučaja uvozi se **piksel, nikad tuđi vektor**: svaka fotografija se ponovno ugradi BABA-inim modelom da padne u isti prostor kao snimke s kamera.

Ljubimci se prepoznaju **preko tijela** (referentne fotke enrollane s pojedinačnih slika); ljudi se imenuju **licem**; vozila prije svega **tablicom** (dolje).

## Vozila: tablica je dokaz, izgled je mišljenje

Vozilo se izgledom prepoznaje slabo — „tamni karavan odozgo" opisuje pola ulice. Zato za vozila postoji jači signal: **registarska tablica**, koju BABA čita sa snimke dok se auto kreće prilazom. Tablica je jedini pravi dokaz identiteta vozila i zato **pobjeđuje svaki zaključak po izgledu** — čak i obrnuto: čisto pročitana tablica koja ne pripada nikome upisanom *skida* ime koje je izgled pogrešno dodijelio.

Upiši tablicu vozilu na njegovoj stranici identiteta i svaki sljedeći dolazak imenuje se od prvog čitanja — vrijedi i za goste.

## Parkirna mjesta

Nacrtaš li scensku regiju s imenom mjesta (P1, P2…), BABA vodi **registar zauzetosti**: mjesto se otvara čim auto stane, a tko je u njemu odlučuje se po tome gdje je stao — imenuje se čim mu se pročita tablica, s dokazom (tablica / izgled / neutvrđeno). „Zauzeto — nepoznato vozilo" je legitiman zapis: bolje pošteno prazno polje nego ime koje se ne može obraniti. **Jedan auto drži najviše jedno mjesto**, a zatvorene epizode daju povijest — tko je bio parkiran, od kada, koliko dugo.

## Boravci (osobe)

Za imenovane osobe BABA vodi **epizode boravka**: tko je na kojoj kameri, od kada, dokad. Epizoda se otvara tek kad prepoznavanje *potraje* (jedan slučajni kadar nije osoba), a zatvara tek kad osoba dokazano ode — viđena je drugdje, kamera je ostala bez ijedne osobe, ili je otišla vlastitim autom (vozilo se može vezati uz vlasnika). Smrt tracka nikad ne zatvara boravak: tracker gubi i nepomičnu osobu, a to nije odlazak. Na stranici identiteta to je sekcija **Boravci**.

## Vrsta pobjeđuje detektor

Detektor je COCO-treniran i zna pogriješiti klasu — robotska kosilica čita kao „pas", mačka kao „pas". Zato identitet nosi **vrstu** kao zasebnu os: kad je subjekt jednom vezan uz identitet, njegova vrsta (pas/mačka/vozilo/uređaj) pobjeđuje ono što detektor kaže na pojedinom kadru. Preimenovanje trackova ne bi pomoglo — sljedeći kadar opet zamijeni; identitet je taj koji zna što subjekt *jest*.

## Cijena licenčne čistoće

Permisivni embedderi (koje smijemo isporučiti komercijalno) na nadzornim licima pri slabom svjetlu daju slab signal. Za privatne deployove to se rješava „donesi svoj model" (BYOM), gdje priložiš jači model na vlastitu odgovornost. Prag poklapanja lica podešava se u Postavke → Prepoznavanje lica.

---

Povezano: [Kako BABA vidi](/help/kako-baba-vidi), [Ugađanje](/help/ugadjanje).
`,
    en: `
Recognition is what sets BABA apart from a recorder: the same person gets the same identity when they return — an hour later, tomorrow, in different clothes. It works through **appearance vectors** (embeddings): a numeric fingerprint that is close for the same person and far for others.

## Face and body — two signals

- **Face** is the strong signal. When BABA sees a face clearly enough, it decides: it links tracks across sessions and names the person.
- **Body** is the weaker, complementary signal. It recognises the same person by their whole appearance (OSNet body re-ID) even when the face isn't visible — hooded, from behind, in the dark.

The key rule: **the body alone must never reach a named identity.** Too many mistakes are possible (two people in a similar jacket). The body may name a person only when it attaches to a track a face has already confirmed — the *face-anchored body chain*. This recognises the same person on frames where their face isn't visible, without the risk of a similar appearance getting someone else's name.

## Naming and reference photos

You name a person by attaching reference photos. The more varied, named photos per person, the more reliable the recognition. BABA imports them from **OPUS · Library** (the family photo library; a library person becomes an identity in one step) or from **Immich** — either way it imports the **pixels, never someone else's vector**: each photo is re-embedded by BABA's own model so it lands in the same space as camera captures.

Pets are recognised **by body** (reference photos enrolled from stills); people are named **by face**; vehicles above all **by plate** (below).

## Vehicles: the plate is proof, appearance is an opinion

A vehicle is recognised poorly by appearance — "a dark estate car seen from above" describes half the street. So vehicles get a stronger signal: the **licence plate**, which BABA reads off the recording while the car moves up the drive. The plate is the only real proof of a vehicle's identity and therefore **beats every appearance-based conclusion** — even in reverse: a cleanly read plate that belongs to nobody enrolled *strips* a name appearance assigned wrongly.

Enter the plate on the vehicle's identity page and every subsequent arrival is named from the first read — guests included.

## Parking places

Draw a scene region carrying a place name (P1, P2…) and BABA keeps an **occupancy registry**: the place opens the moment a car stops and who is in it follows from where it stopped — named as soon as its plate is read, with its evidence (plate / appearance / undetermined). "Occupied — unknown vehicle" is a legitimate record: an honest blank beats a name nobody can defend. **One car holds at most one place**, and closed episodes give the history — who was parked, since when, for how long.

## Stays (people)

For named people BABA keeps **presence episodes**: who is at which camera, since when, until when. An episode opens only when the recognition *sustains* (one stray frame is not a person) and closes only when the person demonstrably left — seen elsewhere, the camera went person-free, or they left in their own car (a vehicle can be linked to its owner). A track's death never closes a stay: the tracker loses even a motionless person, and that is not a departure. On the identity page this is the **Stays** section.

## Species beats the detector

The detector is COCO-trained and will get the class wrong — a robot mower reads as "dog", a cat as "dog". So an identity carries a **species** as a separate axis: once a subject is tied to an identity, its species (dog/cat/vehicle/device) beats what the detector says on any given frame. Relabelling tracks wouldn't help — the next frame flips again; the identity is what knows what the subject *is*.

## The cost of licence cleanliness

Permissive embedders (the ones we may ship commercially) give a weak signal on surveillance faces in poor light. For private deployments this is solved with "bring your own model" (BYOM), where you supply a stronger model at your own risk. The face match threshold is set in Settings → Face recognition.

---

Related: [How BABA sees](/help/kako-baba-vidi), [Tuning](/help/ugadjanje).
`,
  },
};

const UGADJANJE: HelpArticle = {
  slug: "ugadjanje",
  group: "operating",
  title: { hr: "Ugađanje i fantomi", en: "Tuning and phantoms" },
  summary: {
    hr: "Zašto klupa čita kao kamion, kako se to samo popravlja, i kad da uopće diraš pragove.",
    en: "Why a bench reads as a truck, how it self-corrects, and when to touch a threshold at all.",
  },
  body: {
    hr: `
Detektori pri slabom svjetlu i na malim/dalekim objektima znaju **samouvjereno pogriješiti**: klupa pročitana kao kamion s 0.65, svjetiljka kao osoba, sanduk kao auto. To nije kvar kamere ni tvoje krive postavke — to je narav modela. BABA to rješava **sama**, bez da ti moraš loviti pragove.

## Samoučeći registar fantoma

BABA pamti gdje se trackovi *rađaju* i je li se išta rođeno ondje ikad pomaknulo. Mjesto se potiskuje tek kad ispuni tri uvjeta: dovoljno rođenja, **nula movera** (ništa se odande nije odselilo) i raspon dulji od jednog dana. To razlikuje inventar od ljudi — svjetiljka je ondje danima i nikad ne hoda; čovjek koji uđe i stane napravit će nekoliko rođenja, ali kroz minute, ne dane.

Mjesto se poklapa po **stopalu**, ne po veličini okvira — pa ista svjetiljka nacrtana užom kutijom i dalje pada u naučeno mjesto. I ima tvrdu zaštitu: **pouzdana detekcija se nikad ne potiskuje**. Čim track jednom pređe prag stvarne osobe, on je stvaran do kraja — svjetiljka nikad ne dosegne taj prag, čovjek dosegne. Zato osoba koja sjedne točno gdje inače stoji namještaj ipak prolazi.

Sve se **liječi samo**: čim se nešto s potisnutog mjesta pomakne, ono trenutno postaje vidljivo i to mjesto više nikad ne potiskuje. Miči se hidrant, mjesto ostari i nestane.

## Kad diraš pragove

Postavke → Detekcija drži pragove pipelinea. **Zadano: ne diraj ih.** Postavljeni su na izmjereno, i promjena koja popravi jednu situaciju često pokvari drugu.

Kad ipak diraš, promjena se **primjenjuje odmah**, bez restarta, i vrijedi na cijeli pipeline (nema paralelnih ugođaja vidljivih samo u jednom dijelu). Prazno polje znači „vrijedi zadana vrijednost iz konfiguracije". Vrijednost izvan raspona sustav sam odreže — pragovi su ograničeni jer neki od njih ne popuštaju nego *izvrću* zaštitu ako ih preforsiraš.

## Dva praga praćenja

Track se rađa tek iznad praga rođenja, ali se održava na nižem pragu. Zato osoba koja se umiri i kojoj pouzdanost padne i dalje ostaje praćena — track ne umire na svakom slabom kadru. To je jedan od mehanizama koji drže nepomičnu osobu na životu.

---

Povezano: [Zone](/help/zone), [Kako BABA vidi](/help/kako-baba-vidi).
`,
    en: `
Detectors in poor light and on small/distant objects will **confidently get it wrong**: a bench read as a truck at 0.65, a lamp as a person, a crate as a car. That is not a camera fault or a setting you got wrong — it is the nature of the model. BABA handles it **itself**, without you having to chase thresholds.

## The self-learning phantom registry

BABA remembers where tracks are *born* and whether anything born there ever moved. A spot is suppressed only when it meets three conditions: enough births, **zero movers** (nothing ever left it), and a span longer than a day. That separates inventory from people — a lamp is there for days and never walks; a person who enters and stands will produce a few births, but over minutes, not days.

A spot is matched by the **foot**, not the box size — so the same lamp drawn with a narrower box still lands on the learned spot. And it has a hard safeguard: **a confident detection is never suppressed**. Once a track crosses the real-person bar even once, it is real for the rest of its life — a lamp never reaches that bar, a person does. That is why a person who sits down exactly where furniture usually stands still gets through.

It all **self-heals**: the instant anything moves off a suppressed spot, it becomes visible at once and that spot never suppresses again. Move the hydrant, and the spot ages out and disappears.

## When to touch a threshold

Settings → Detection holds the pipeline thresholds. **Default: leave them alone.** They are set to measured values, and a change that fixes one situation often breaks another.

When you do change one, it **applies immediately**, without a restart, and across the whole pipeline (no parallel tweaks visible in only one part). An empty field means "the deployment default applies". A value out of range is clamped — the thresholds are bounded because some of them don't loosen but *invert* a safeguard if pushed too far.

## Two tracking thresholds

A track is born only above the birth threshold, but maintained at a lower one. So a person who settles and whose confidence drops stays tracked — the track doesn't die on every weak frame. It is one of the mechanisms that keep a stationary person alive.

---

Related: [Zones](/help/zone), [How BABA sees](/help/kako-baba-vidi).
`,
  },
};

const SNIMANJE: HelpArticle = {
  slug: "snimanje",
  group: "operating",
  title: { hr: "Snimanje i pohrana", en: "Recording and storage" },
  summary: {
    hr: "Zašto se snima 24/7 neovisno o detekciji, kako se klipovi režu i kako se čuva prostor.",
    en: "Why recording is 24/7 regardless of detection, how clips are cut, and how space is kept.",
  },
  body: {
    hr: `
Kamere snimaju **kontinuirano, 24/7**, bez obzira vidi li BABA išta — jedan tok po kameri, rezan na segmente od 5 minuta, nikad uvjetovan detekcijom. Zato nikad ne propustiš trenutak zato što se pipeline na tren zabunio. Aktivnost je razumijevanje *iznad* te snimke, ne zamjena za nju.

## Način snimanja: kontinuirano vs. aktivnost

U Postavke → Snimanje biraš jedan od dva načina. **Bitno:** oba i dalje snimaju 24/7 — način nije prekidač snimanja nego **politika čuvanja** (što se poslije zadrži, a što obriše).

- **Kontinuirano** — čuva se sve, ograničeno samo starošću (zadano 7 dana) i sigurnosnom granicom diska. Puna povijest.
- **Aktivnost** — nakon kratkog prozora briše segmente koji se ne preklapaju ni s jednim posjetom, i drži samo **kratki obrub oko aktivnosti** (pre/post-roll, zadano 1 minuta). Tako se disk štedi, ali tihi dijelovi dana nisu trajno sačuvani.

## Tri sloja čuvanja

1. **Starost** — segmenti stariji od \`retention_days\` (zadano 7) se brišu, u oba načina.
2. **Aktivnost** — samo u aktivnom načinu; briše tihe segmente kako je gore opisano.
3. **Disk** — sigurnosna mreža: kad zauzeće prijeđe gornju granicu (zadano 85%), briše se najstarije dok se ne spusti na donju (75%). Ima i osigurač: ako brisanje ne oslobađa prostor (netko drugi puni dijeljeni disk), staje i glasno javi umjesto da pobriše cijelu povijest.

## Klipovi se režu na zahtjev

Ne postoji poseban „snimak eventa". Kad u Aktivnosti klikneš redak, BABA izreže klip iz kontinuiranih segmenata za točan prozor tog trenutka i kešira ga. Zato dolazak i odlazak vozila daju dva kratka klipa iz iste snimke. H.264 kamere se režu bez ponovnog kodiranja (lossless); HEVC se pretvara u H.264. Keš klipova čisti se nakon 6 sati.

## Dva sloja pohrane

- **Spori sloj** (HDD/polje) drži glavni video — kontinuirane segmente. Veliko, sekvencijalni I/O. Ovdje se mjeri i granica diska za retenciju.
- **Brzi sloj** (SSD/NVMe) drži male datoteke slučajnog pristupa: keširane klipove, cropove, sličice, referentne fotografije — te baza, modeli i logovi.

Tako veliki video ne opterećuje isti disk kao latenciju-osjetljiva baza.

---

Povezano: [Aktivnost i posjeti](/help/aktivnost-i-posjeti).
`,
    en: `
Cameras record **continuously, 24/7**, whether or not BABA sees anything — one stream per camera, cut into 5-minute segments, never gated on detection. So you never miss a moment because the pipeline was briefly wrong. Activity is understanding *on top of* that footage, not a replacement for it.

## Recording mode: continuous vs. activity

In Settings → Recording you pick one of two modes. **Important:** both still record 24/7 — the mode is not a capture switch but a **retention policy** (what is kept afterwards, and what is deleted).

- **Continuous** — everything is kept, bounded only by age (default 7 days) and the disk safety net. Full history.
- **Activity** — past a short window, segments that overlap no visit are deleted, and only a **short margin around activity** is kept (pre/post-roll, default 1 minute). This saves disk, but the quiet parts of the day are not kept permanently.

## Three layers of retention

1. **Age** — segments older than \`retention_days\` (default 7) are deleted, in both modes.
2. **Activity** — activity mode only; deletes quiet segments as described above.
3. **Disk** — a safety net: when usage crosses the high-water mark (default 85%), the oldest is deleted until it drops to the low mark (75%). It also has a fuse: if deleting frees no space (someone else is filling a shared disk), it stops and alarms rather than wiping all history.

## Clips are cut on demand

There is no separate "event recording". When you click a row in Activity, BABA cuts a clip from the continuous segments for the exact window of that moment and caches it. That is why a vehicle's arrival and departure yield two short clips from the same footage. H.264 cameras are cut without re-encoding (lossless); HEVC is converted to H.264. The clip cache is cleared after 6 hours.

## Two storage tiers

- **Slow tier** (HDD/array) holds the bulk video — the continuous segments. Large, sequential I/O. The disk retention limit is measured here.
- **Fast tier** (SSD/NVMe) holds small random-access files: cached clips, crops, thumbnails, reference photos — plus the database, models and logs.

So bulk video doesn't compete with the latency-sensitive database on one disk.

---

Related: [Activity and visits](/help/aktivnost-i-posjeti).
`,
  },
};

export const HELP_ARTICLES: HelpArticle[] = [
  KAKO_BABA_VIDI,
  AKTIVNOST_I_POSJETI,
  ZONE,
  IDENTITETI,
  UGADJANJE,
  SNIMANJE,
];

export function articleBySlug(slug: string): HelpArticle | undefined {
  return HELP_ARTICLES.find((a) => a.slug === slug);
}
