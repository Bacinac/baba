BABA nije klasičan snimač. Klasičan NVR snima piksele i čeka da ih čovjek pregleda; BABA **razumije scenu u stvarnom vremenu** — vidi tko je došao, prati ga dok je prisutan, prepozna ga kad se vrati, i zapisuje to kao događaje, ne kao sate videa.

Sve što vidiš u aplikaciji izlazi iz jednog toka. Vrijedi ga razumjeti jer objašnjava zašto je nešto zapisano, a nešto nije.

## Tok obrade

1. **Kamera → dekodiranje.** Svaka kamera daje RTSP tok koji BABA dekodira jednom i tu jednu sliku dijeli svemu nizvodno.
2. **Detektor.** Na svakom kadru transformer-model traži objekte — osobu, vozilo, životinju — i vraća okvir s pouzdanošću. Ovo je *po kadru*: detektor ne zna da je osoba na ovom kadru ista kao na prošlom.
3. **Tracker.** Povezuje detekcije kroz kadrove u **trackove**: jedna osoba koja se kreće scenom = jedan track, čak i kad je detektor na tren izgubi. Ovdje se rađa pojam „prisustva”.
4. **Prepoznavanje (re-ID).** Za svaki track BABA računa vektor izgleda tijela (i lica, ako ga vidi). Time povezuje isti subjekt kroz vrijeme i kamere — ista osoba prepoznata kad se vrati, i sutra, u drugoj jakni.
5. **Event-manager.** Odlučuje što je vrijedno zapisati: ulazak u zonu, zadržavanje, dolazak vozila, kraj posjeta. To puni **Aktivnost**.
6. **Snimanje.** Teče kontinuirano 24/7 neovisno o detekciji; koliko se te snimke zadrži određuje politika čuvanja (vidi [Snimanje](/help/recording)). Aktivnost je sloj razumijevanja iznad nje.

## Detekcija nije track

Ovo je razlika koju vrijedi zapamtiti. **Detekcija** je ono što model vidi na jednom kadru — sirovo, može treperiti, može se prevariti (klupa pročitana kao osoba). **Track** je ono što tracker sastavi kroz vrijeme, i tek to postaje prisustvo koje se prati i zapisuje.

Zbog toga Zone editor pokazuje sirove detekcije (da vidiš što model kaže dok kalibriraš), a Aktivnost pokazuje posjete (ono što je preživjelo). Kad se to dvoje razlikuje, ne znači kvar — znači da je pipeline nešto odbacio, i BABA ti to i sivi u pregledu.

## Zašto nema detekcije pokreta

Većina NVR-ova pali analizu tek kad se nešto pomakne. To ima jednu fatalnu manu: **gubi osobu koja stoji**. Tko se umiri — sjedne, čeka, promatra — za takav sustav prestaje postojati.

BABA namjerno ne radi tako. Detektor gleda svaki kadar bez obzira na kretanje, a tracker drži osobu koja se umiri preko izgleda, ne preko pomaka. Pouzdano vidjeti nepomičnu osobu je temeljni zahtjev, ne dodatak — i cijeli niz odluka u sustavu postoji da se ta osoba nikad ne izgubi.

---

Dalje: kako prisustvo postaje zapis u [Aktivnost i posjeti](/help/activity-and-visits); kako se odlučuje što je gdje bitno u [Zone](/help/zones); kako BABA zna tko je tko u [Identiteti i prepoznavanje](/help/identities).
