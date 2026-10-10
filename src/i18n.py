# SPDX-License-Identifier: Apache-2.0
"""Curated ISO 639-1 languages and explicit document translations."""

from dataclasses import dataclass

DEFAULT_LANGUAGE = 'da'


@dataclass(frozen=True)
class Language:
    name: str
    summary: tuple[str, str, str, str]
    speaker: str
    unknown: str
    notes: str
    research: str
    searched: str
    not_searched: str
    sources: str
    unclear: str


LANGUAGES = {
    'ar': Language(
        name='Arabic',
        summary=(
            'النقاط الرئيسية', 'القرارات', 'الخطوات التالية', 'نقاط الغموض',
        ),
        speaker='المتحدث', unknown='غير معروف', notes='ملاحظات',
        research='بحث في الخلفية',
        searched='تم إجراء بحث على الويب.',
        not_searched='لم يتم إجراء بحث على الويب.',
        sources='مصادر الويب', unclear='[غير واضح]',
    ),
    'bg': Language(
        name='Bulgarian',
        summary=(
            'Основни моменти', 'Решения', 'Следващи стъпки', 'Неясноти',
        ),
        speaker='Говорещ', unknown='Неизвестен', notes='Бележки',
        research='Проучване на контекста',
        searched='Извършено е търсене в интернет.',
        not_searched='Не е извършено търсене в интернет.',
        sources='Интернет източници', unclear='[неясно]',
    ),
    'bn': Language(
        name='Bengali',
        summary=('মূল বিষয়', 'সিদ্ধান্ত', 'পরবর্তী পদক্ষেপ', 'অনিশ্চয়তা'),
        speaker='বক্তা', unknown='অজানা', notes='নোট',
        research='পটভূমি অনুসন্ধান',
        searched='ওয়েবে অনুসন্ধান করা হয়েছে।',
        not_searched='ওয়েবে অনুসন্ধান করা হয়নি।',
        sources='ওয়েব উৎস', unclear='[অস্পষ্ট]',
    ),
    'cs': Language(
        name='Czech',
        summary=('Hlavní body', 'Rozhodnutí', 'Další kroky', 'Nejasnosti'),
        speaker='Mluvčí', unknown='Neznámý', notes='Poznámky',
        research='Vyhledání souvislostí',
        searched='Vyhledávání na webu provedeno.',
        not_searched='Vyhledávání na webu nebylo provedeno.',
        sources='Webové zdroje', unclear='[nesrozumitelné]',
    ),
    'da': Language(
        name='Danish',
        summary=(
            'Vigtige punkter', 'Beslutninger', 'Næste skridt', 'Usikkerheder',
        ),
        speaker='Taler', unknown='Ukendt', notes='Noter',
        research='Baggrundsopslag',
        searched='Webopslag udført.',
        not_searched='Intet webopslag udført.',
        sources='Webkilder', unclear='[uklart]',
    ),
    'de': Language(
        name='German',
        summary=(
            'Wichtige Punkte', 'Entscheidungen', 'Nächste Schritte',
            'Unklarheiten',
        ),
        speaker='Sprecher', unknown='Unbekannt', notes='Anmerkungen',
        research='Hintergrundrecherche',
        searched='Webrecherche durchgeführt.',
        not_searched='Keine Webrecherche durchgeführt.',
        sources='Webquellen', unclear='[unverständlich]',
    ),
    'el': Language(
        name='Greek',
        summary=(
            'Βασικά σημεία', 'Αποφάσεις', 'Επόμενα βήματα', 'Αβεβαιότητες',
        ),
        speaker='Ομιλητής', unknown='Άγνωστος', notes='Σημειώσεις',
        research='Έρευνα υποβάθρου',
        searched='Πραγματοποιήθηκε αναζήτηση στο διαδίκτυο.',
        not_searched='Δεν πραγματοποιήθηκε αναζήτηση στο διαδίκτυο.',
        sources='Διαδικτυακές πηγές', unclear='[ακατάληπτο]',
    ),
    'en': Language(
        name='English',
        summary=('Key points', 'Decisions', 'Next steps', 'Uncertainties'),
        speaker='Speaker', unknown='Unknown', notes='Notes',
        research='Background lookup',
        searched='Web lookup performed.',
        not_searched='No web lookup performed.',
        sources='Web sources', unclear='[unclear]',
    ),
    'es': Language(
        name='Spanish',
        summary=(
            'Puntos clave', 'Decisiones', 'Próximos pasos', 'Incertidumbres',
        ),
        speaker='Hablante', unknown='Desconocido', notes='Notas',
        research='Consulta de contexto',
        searched='Se realizó una búsqueda en la web.',
        not_searched='No se realizó una búsqueda en la web.',
        sources='Fuentes web', unclear='[ininteligible]',
    ),
    'fi': Language(
        name='Finnish',
        summary=(
            'Keskeiset kohdat', 'Päätökset', 'Seuraavat vaiheet',
            'Epävarmuudet',
        ),
        speaker='Puhuja', unknown='Tuntematon', notes='Huomautukset',
        research='Taustahaku',
        searched='Verkkohaku tehtiin.',
        not_searched='Verkkohakua ei tehty.',
        sources='Verkkolähteet', unclear='[epäselvää]',
    ),
    'fr': Language(
        name='French',
        summary=(
            'Points clés', 'Décisions', 'Prochaines étapes', 'Incertitudes',
        ),
        speaker='Locuteur', unknown='Inconnu', notes='Notes',
        research='Recherche de contexte',
        searched='Recherche web effectuée.',
        not_searched='Aucune recherche web effectuée.',
        sources='Sources web', unclear='[inaudible]',
    ),
    'hi': Language(
        name='Hindi',
        summary=('मुख्य बिंदु', 'निर्णय', 'अगले कदम', 'अनिश्चितताएँ'),
        speaker='वक्ता', unknown='अज्ञात', notes='टिप्पणियाँ',
        research='पृष्ठभूमि खोज',
        searched='वेब खोज की गई।',
        not_searched='वेब खोज नहीं की गई।',
        sources='वेब स्रोत', unclear='[अस्पष्ट]',
    ),
    'hu': Language(
        name='Hungarian',
        summary=(
            'Főbb pontok', 'Döntések', 'Következő lépések',
            'Bizonytalanságok',
        ),
        speaker='Beszélő', unknown='Ismeretlen', notes='Megjegyzések',
        research='Háttérkeresés',
        searched='Webes keresés történt.',
        not_searched='Nem történt webes keresés.',
        sources='Webes források', unclear='[érthetetlen]',
    ),
    'it': Language(
        name='Italian',
        summary=(
            'Punti chiave', 'Decisioni', 'Prossimi passi', 'Incertezze',
        ),
        speaker='Parlante', unknown='Sconosciuto', notes='Note',
        research='Ricerca di contesto',
        searched='Ricerca web effettuata.',
        not_searched='Nessuna ricerca web effettuata.',
        sources='Fonti web', unclear='[incomprensibile]',
    ),
    'ja': Language(
        name='Japanese',
        summary=('要点', '決定事項', '次のステップ', '不確実な点'),
        speaker='話者', unknown='不明', notes='注記',
        research='背景調査',
        searched='ウェブ検索を実施しました。',
        not_searched='ウェブ検索は実施していません。',
        sources='ウェブ情報源', unclear='[聞き取り不能]',
    ),
    'nl': Language(
        name='Dutch',
        summary=(
            'Kernpunten', 'Besluiten', 'Volgende stappen', 'Onzekerheden',
        ),
        speaker='Spreker', unknown='Onbekend', notes='Notities',
        research='Achtergrondonderzoek',
        searched='Webonderzoek uitgevoerd.',
        not_searched='Geen webonderzoek uitgevoerd.',
        sources='Webbronnen', unclear='[onverstaanbaar]',
    ),
    'no': Language(
        name='Norwegian',
        summary=(
            'Viktige punkter', 'Beslutninger', 'Neste steg', 'Usikkerheter',
        ),
        speaker='Taler', unknown='Ukjent', notes='Merknader',
        research='Bakgrunnsoppslag',
        searched='Nettsøk utført.',
        not_searched='Ingen nettsøk utført.',
        sources='Nettkilder', unclear='[uklart]',
    ),
    'pl': Language(
        name='Polish',
        summary=(
            'Kluczowe punkty', 'Decyzje', 'Następne kroki', 'Niepewności',
        ),
        speaker='Mówca', unknown='Nieznany', notes='Uwagi',
        research='Wyszukiwanie kontekstu',
        searched='Przeprowadzono wyszukiwanie w internecie.',
        not_searched='Nie przeprowadzono wyszukiwania w internecie.',
        sources='Źródła internetowe', unclear='[niezrozumiałe]',
    ),
    'pt': Language(
        name='Portuguese',
        summary=(
            'Pontos principais', 'Decisões', 'Próximos passos', 'Incertezas',
        ),
        speaker='Falante', unknown='Desconhecido', notes='Notas',
        research='Pesquisa de contexto',
        searched='Pesquisa na web realizada.',
        not_searched='Nenhuma pesquisa na web realizada.',
        sources='Fontes da web', unclear='[inaudível]',
    ),
    'ro': Language(
        name='Romanian',
        summary=(
            'Puncte cheie', 'Decizii', 'Pașii următori', 'Incertitudini',
        ),
        speaker='Vorbitor', unknown='Necunoscut', notes='Note',
        research='Cercetare de context',
        searched='Căutare web efectuată.',
        not_searched='Nu s-a efectuat nicio căutare web.',
        sources='Surse web', unclear='[neinteligibil]',
    ),
    'ru': Language(
        name='Russian',
        summary=(
            'Ключевые моменты', 'Решения', 'Следующие шаги', 'Неясности',
        ),
        speaker='Говорящий', unknown='Неизвестный', notes='Примечания',
        research='Поиск контекста',
        searched='Поиск в интернете выполнен.',
        not_searched='Поиск в интернете не выполнялся.',
        sources='Интернет-источники', unclear='[неразборчиво]',
    ),
    'sk': Language(
        name='Slovak',
        summary=('Hlavné body', 'Rozhodnutia', 'Ďalšie kroky', 'Nejasnosti'),
        speaker='Rečník', unknown='Neznámy', notes='Poznámky',
        research='Vyhľadanie súvislostí',
        searched='Vyhľadávanie na webe vykonané.',
        not_searched='Vyhľadávanie na webe nebolo vykonané.',
        sources='Webové zdroje', unclear='[nezrozumiteľné]',
    ),
    'sv': Language(
        name='Swedish',
        summary=('Viktiga punkter', 'Beslut', 'Nästa steg', 'Osäkerheter'),
        speaker='Talare', unknown='Okänd', notes='Anteckningar',
        research='Bakgrundssökning',
        searched='Webbsökning utförd.',
        not_searched='Ingen webbsökning utförd.',
        sources='Webbkällor', unclear='[ohörbart]',
    ),
    'tr': Language(
        name='Turkish',
        summary=(
            'Önemli noktalar', 'Kararlar', 'Sonraki adımlar', 'Belirsizlikler',
        ),
        speaker='Konuşmacı', unknown='Bilinmeyen', notes='Notlar',
        research='Bağlam araştırması',
        searched='Web araması yapıldı.',
        not_searched='Web araması yapılmadı.',
        sources='Web kaynakları', unclear='[anlaşılmıyor]',
    ),
    'uk': Language(
        name='Ukrainian',
        summary=(
            'Ключові моменти', 'Рішення', 'Наступні кроки', 'Невизначеності',
        ),
        speaker='Мовець', unknown='Невідомий', notes='Примітки',
        research='Пошук контексту',
        searched='Пошук в інтернеті виконано.',
        not_searched='Пошук в інтернеті не виконувався.',
        sources='Інтернет-джерела', unclear='[нерозбірливо]',
    ),
    'zh': Language(
        name='Chinese',
        summary=('要点', '决定', '后续步骤', '不确定事项'),
        speaker='说话人', unknown='未知', notes='备注',
        research='背景查询',
        searched='已进行网络搜索。',
        not_searched='未进行网络搜索。',
        sources='网络来源', unclear='[听不清]',
    ),
}


def language_config(code):
    if not isinstance(code, str) or code not in LANGUAGES:
        raise ValueError(
            'language must be a supported ISO 639-1 code: '
            + ', '.join(LANGUAGES)
            + '.'
        )
    return LANGUAGES[code]
