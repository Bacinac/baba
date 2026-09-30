Zona je poligon koji crtaš na slici kamere da kažeš „ovdje mi je stalo”. Sve zone rade po istom sidru: **stopalo subjekta** — donji centar okvira, mjesto gdje objekt dodiruje tlo. Poligon nacrtan jednom ponaša se isto svugdje gdje se procjenjuje, jer se uvijek pita je li *stopalo* unutra, ne cijela kutija.

## Vrste zona

- **Zona interesa / ulaza** — ovdje želiš događaje: netko ušao, netko se zadržao. Ovo je najčešća zona.
- **Parkirna zona** — mjesto gdje vozila legitimno stoje danima. Ondje se parkirani auto ne potiskuje kao smeće (vidi [Ugađanje](/help/tuning)), a trajanje se mjeri kao parkirano vrijeme.
- **Zabranjena zona (restricted)** — mjesto gdje ništa ne bi smjelo biti. Za razliku od zone interesa, ova okida i na **nepomične** objekte: nešto ostavljeno ondje i dalje je događaj.
- **Ignore zona** — mrtvo tlo. Susjedova terasa, javni pločnik. Ovdje BABA ne prati **ništa** — detekcije se odbacuju na ulazu u tracker, pa ne troše ni prepoznavanje ni snimku eventa.

Ignore zona nije rješenje za statično smeće (to rješava samoučeći registar fantoma) — ona je za mjesta gdje se *stvarni* subjekti pojavljuju, a ti ih jednostavno ne želiš.

## Pravila po klasi

Svaka zona može imati pravila po klasi: minimalna pouzdanost, minimalna veličina okvira, minimalno zadržavanje prije nego se javi event, i cooldown da isti prolaz ne okida dvaput. Time gasiš npr. auto na cesti u kutu kadra, a zadržavaš ljude na ulazu.

## Motion gate — i zašto osoba prolazi

Parkirna, ulazna i interesna zona po zadanom **ne okidaju na nepomične objekte**: podrhtavanje okvira parkiranog auta preko ruba poligona inače pravi beskonačan niz ulaz/izlaz. Ali za **osobu je to isključeno**: netko tko se zaustavi u zoni interesa ne miruje slučajno — to je zadržavanje, najizvještajnija stvar koju kamera može vidjeti. Vozila zadržavaju gate, ljudi ne.

## Pregled sirovog feeda

Zone editor namjerno prikazuje **sirove detekcije** (izlaz detektora, prije trackera) da vidiš što model govori dok crtaš i kalibriraš. Ono što pipeline već odbacuje — statični fantom, detekcija u ignore zoni — prikazano je sivo i iscrtkano, s razlogom. Sivo znači „ovo ne ide dalje”, ne kvar.

---

Povezano: [Kako BABA vidi](/help/how-baba-sees), [Ugađanje i fantomi](/help/tuning).
