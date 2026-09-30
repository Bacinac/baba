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

Povezano: [Kako BABA vidi](/help/how-baba-sees), [Ugađanje](/help/tuning).
