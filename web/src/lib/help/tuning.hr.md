Detektori pri slabom svjetlu i na malim/dalekim objektima znaju **samouvjereno pogriješiti**: klupa pročitana kao kamion s 0.65, svjetiljka kao osoba, sanduk kao auto. To nije kvar kamere ni tvoje krive postavke — to je narav modela. BABA to rješava **sama**, bez da ti moraš loviti pragove.

## Samoučeći registar fantoma

BABA pamti gdje se trackovi *rađaju* i je li se išta rođeno ondje ikad pomaknulo. Mjesto se potiskuje tek kad ispuni tri uvjeta: dovoljno rođenja, **nula movera** (ništa se odande nije odselilo) i raspon dulji od jednog dana. To razlikuje inventar od ljudi — svjetiljka je ondje danima i nikad ne hoda; čovjek koji uđe i stane napravit će nekoliko rođenja, ali kroz minute, ne dane.

Mjesto se poklapa po **stopalu**, ne po veličini okvira — pa ista svjetiljka nacrtana užom kutijom i dalje pada u naučeno mjesto. I ima tvrdu zaštitu: **pouzdana detekcija se nikad ne potiskuje**. Čim track jednom pređe prag stvarne osobe, on je stvaran do kraja — svjetiljka nikad ne dosegne taj prag, čovjek dosegne. Zato osoba koja sjedne točno gdje inače stoji namještaj ipak prolazi.

Sve se **liječi samo**: čim se nešto s potisnutog mjesta pomakne, ono trenutno postaje vidljivo i to mjesto više nikad ne potiskuje. Miči se hidrant, mjesto ostari i nestane.

## Kad diraš pragove

Postavke → Detekcija drži pragove pipelinea. **Zadano: ne diraj ih.** Postavljeni su na izmjereno, i promjena koja popravi jednu situaciju često pokvari drugu.

Kad ipak diraš, promjena se **primjenjuje odmah**, bez restarta, i vrijedi na cijeli pipeline (nema paralelnih ugođaja vidljivih samo u jednom dijelu). Prazno polje znači „vrijedi zadana vrijednost iz konfiguracije”. Vrijednost izvan raspona sustav sam odreže — pragovi su ograničeni jer neki od njih ne popuštaju nego *izvrću* zaštitu ako ih preforsiraš.

## Dva praga praćenja

Track se rađa tek iznad praga rođenja, ali se održava na nižem pragu. Zato osoba koja se umiri i kojoj pouzdanost padne i dalje ostaje praćena — track ne umire na svakom slabom kadru. To je jedan od mehanizama koji drže nepomičnu osobu na životu.

---

Povezano: [Zone](/help/zones), [Kako BABA vidi](/help/how-baba-sees).
