// Croatian names for the detector's COCO-80 vocabulary.
//
// The class name arrives from the detector as a raw English COCO token and is
// shown to the operator in four places: the global / per-camera / per-zone
// detection-rule cards and the live overlay label. Everything else in the UI
// is translated, so an untranslated "potted plant" on a Croatian screen reads
// as a bug — especially on the rule cards, where the junk classes (bench,
// suitcase, couch) are exactly the ones an operator goes hunting for after a
// phantom detection.
//
// Kept apart from `lib/i18n/` on purpose: that catalogue is UI copy, this
// is a fixed vocabulary that comes from the model, not from us. The English
// side needs no table — the COCO token IS the English label.
import { i18n } from "$lib/kit";
const HR: Record<string, string> = {
  person: "osoba",
  bicycle: "bicikl",
  car: "automobil",
  motorcycle: "motocikl",
  airplane: "zrakoplov",
  bus: "autobus",
  train: "vlak",
  truck: "kamion",
  boat: "brod",
  "traffic light": "semafor",
  "fire hydrant": "hidrant",
  "stop sign": "znak stop",
  "parking meter": "parkirni automat",
  bench: "klupa",
  bird: "ptica",
  cat: "mačka",
  dog: "pas",
  horse: "konj",
  sheep: "ovca",
  cow: "krava",
  elephant: "slon",
  bear: "medvjed",
  zebra: "zebra",
  giraffe: "žirafa",
  backpack: "ruksak",
  umbrella: "kišobran",
  handbag: "torbica",
  tie: "kravata",
  suitcase: "kovčeg",
  frisbee: "frizbi",
  skis: "skije",
  snowboard: "daska za snijeg",
  "sports ball": "lopta",
  kite: "zmaj",
  "baseball bat": "bejzbolska palica",
  "baseball glove": "bejzbolska rukavica",
  skateboard: "skejtbord",
  surfboard: "daska za surfanje",
  "tennis racket": "teniski reket",
  bottle: "boca",
  "wine glass": "čaša za vino",
  cup: "šalica",
  fork: "vilica",
  knife: "nož",
  spoon: "žlica",
  bowl: "zdjela",
  banana: "banana",
  apple: "jabuka",
  sandwich: "sendvič",
  orange: "naranča",
  broccoli: "brokula",
  carrot: "mrkva",
  "hot dog": "hot dog",
  pizza: "pizza",
  donut: "krafna",
  cake: "kolač",
  chair: "stolica",
  couch: "kauč",
  "potted plant": "biljka u posudi",
  bed: "krevet",
  "dining table": "blagovaonski stol",
  toilet: "WC školjka",
  tv: "televizor",
  laptop: "prijenosno računalo",
  mouse: "miš",
  remote: "daljinski upravljač",
  keyboard: "tipkovnica",
  "cell phone": "mobitel",
  microwave: "mikrovalna pećnica",
  oven: "pećnica",
  toaster: "toster",
  sink: "sudoper",
  refrigerator: "hladnjak",
  book: "knjiga",
  clock: "sat",
  vase: "vaza",
  scissors: "škare",
  "teddy bear": "plišani medvjedić",
  "hair drier": "sušilo za kosu",
  toothbrush: "četkica za zube",
};

/** Operator-facing name for a raw COCO class token. */
export function classLabel(name: string | null | undefined): string {
  if (!name) return "";
  if (i18n.locale !== "hr") return name;
  return HR[name] ?? name;
}
