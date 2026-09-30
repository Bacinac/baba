Kamere snimaju **kontinuirano, 24/7**, bez obzira vidi li BABA išta — jedan tok po kameri, rezan na segmente od 5 minuta, nikad uvjetovan detekcijom. Zato nikad ne propustiš trenutak zato što se pipeline na tren zabunio. Aktivnost je razumijevanje *iznad* te snimke, ne zamjena za nju.

## Način snimanja: kontinuirano vs. aktivnost

U Postavke → Snimanje biraš jedan od dva načina. **Bitno:** oba i dalje snimaju 24/7 — način nije prekidač snimanja nego **politika čuvanja** (što se poslije zadrži, a što obriše).

- **Kontinuirano** — čuva se sve, ograničeno samo starošću (zadano 7 dana) i sigurnosnom granicom diska. Puna povijest.
- **Aktivnost** — nakon kratkog prozora briše segmente koji se ne preklapaju ni s jednim posjetom, i drži samo **kratki obrub oko aktivnosti** (pre/post-roll, zadano 1 minuta). Tako se disk štedi, ali tihi dijelovi dana nisu trajno sačuvani.

## Tri sloja čuvanja

1. **Starost** — segmenti stariji od `retention_days` (zadano 7) se brišu, u oba načina.
2. **Aktivnost** — samo u aktivnom načinu; briše tihe segmente kako je gore opisano.
3. **Disk** — sigurnosna mreža: kad zauzeće prijeđe gornju granicu (zadano 85%), briše se najstarije dok se ne spusti na donju (75%). Ima i osigurač: ako brisanje ne oslobađa prostor (netko drugi puni dijeljeni disk), staje i glasno javi umjesto da pobriše cijelu povijest.

## Klipovi se režu na zahtjev

Ne postoji poseban „snimak eventa". Kad u Aktivnosti klikneš redak, BABA izreže klip iz kontinuiranih segmenata za točan prozor tog trenutka i kešira ga. Zato dolazak i odlazak vozila daju dva kratka klipa iz iste snimke. H.264 kamere se režu bez ponovnog kodiranja (lossless); HEVC se pretvara u H.264. Keš klipova čisti se nakon 6 sati.

## Dva sloja pohrane

- **Spori sloj** (HDD/polje) drži glavni video — kontinuirane segmente. Veliko, sekvencijalni I/O. Ovdje se mjeri i granica diska za retenciju.
- **Brzi sloj** (SSD/NVMe) drži male datoteke slučajnog pristupa: keširane klipove, cropove, sličice, referentne fotografije — te baza, modeli i logovi.

Tako veliki video ne opterećuje isti disk kao latenciju-osjetljiva baza.

---

Povezano: [Aktivnost i posjeti](/help/activity-and-visits).
