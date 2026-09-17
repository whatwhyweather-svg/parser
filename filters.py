"""Фильтры постов: мусор, русский язык, поиск SEO/GEO-подрядчика."""

from __future__ import annotations

import re

from hashtags import clean_post_text, normalize_hashtag, text_mentions_tag

CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-z]")
# Украина: этих букв нет в русском.
UKR_LETTER_RE = re.compile(r"[іїєґІЇЄҐ]")
UKR_HINT_RE = re.compile(
    r"(?iu)(?<![\wА-Яа-яЁёіїєґ])"
    r"(вартість|напишіть|будь\s*ласка|дякую|якщо|будь-який|"
    r"будьякий|щось|також|можна)"
    r"(?![\wА-Яа-яЁёіїєґ])"
)

# Мусор / спам / донаты / витрина товара / UI-хром Threads
JUNK_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"onlyfans|fansly|crypto\s*giveaway|free\s*nitro",
        r"подписывайся\s+на\s+канал.*заработок",
        r"100%\s*гарантия.*пассивн",
        r"click\s+here|link\s+in\s+bio\s+now",
        r"(?:t\.me|telegram)[^\s]{0,40}(?:casino|ставки|казино)",
        r"пожертв\w*|донат\w*|реквизит\w*\s+для\s+(?:помощ|перевод)",
        r"сбербанк|тинькофф|тиньков|альфа[\s\-]?банк|яндекс[\s\-]?кошел",
        r"(?:карт[аыеу]\s*(?:мир|visa|mastercard)|номер\s+карт)",
        r"\b2202\d{12}\b|\b5536\d{12}\b|\b4100\d{10,}\b",
        r"приют|помощь\s+животн|оплатить\s+коммунал|долг\s+за\s+свет",
        r"взаимн\w*\s+подпис|взаимка",
        # обрезки интерфейса вместо текста поста
        r"поставить\s*[«\"']?нравится",
        r"сделать\s+репост",
        r"подписаться\w{0,40}\d+\s*дн",
        r"ответ\d+сделать\s*репост",
    )
]

# Не лид: чужой заказ / вовлечение / блог про «я сам сел за SEO»
NOT_A_LEAD_RE = re.compile(
    r"(?iu)(?:"
    # реферал на третьих лиц
    r"им\s+нуж\w*|им\s+требуется|им\s+ищ\w*|"
    r"постучитесь|постучись\s+в|"
    r"у\s+них\s+(?:нужен|ищут|требуется)|"
    r"напишите\s+(?:им|туда|в\s+шато)|"
    r"перешлите\s+(?:им|контакт)|"
    r"отмечайте\s+в\s+коммент|отмечай\w*\s+в\s+коммент|тегайте\s+в\s+коммент|"
    # вовлечение / блог, не заказ подрядчика
    r"кто[\-\s]?то\s+тоже|кто\s+ещё\s+сейчас|"
    r"а\s+вы\s+тоже|что\s+уже\s+пробовали|"
    r"есть\s+у\s+меня\s+сервис|у\s+меня\s+есть\s+сервис|"
    r"сел\s+за\s+(?:seo|сео)|наконец\s+сел\s+за|"
    r"пишу\s+(?:отдельн\w*\s+)?(?:стать|гайд|материал)|"
    r"вместо\s+одной\s+большой\s+статьи|"
    r"гоняется?\s+за\s+(?:ai[\s\-]?видимост|видимостью)|"
    r"первое[,\s]+что\s+понял|"
    r"находка[,\s]+которая\s+удивила|"
    r"как\s+выгляд\w*.{0,40}(?:seo|сео)|"
    r"первый\s+месяц.{0,40}(?:трафик|стать)|"
    r"если\s+обещают\s+клиент|"
    r"признавайтесь|исповеду|"
    r"давайте\s+признаемся"
    r")"
)


# Явный оффтоп: аренда, знакомства, бьюти, курсы IT — не SEO-лид
OFFTOPIC_JUNK_RE = re.compile(
    r"(?iu)(?:"
    r"знакомств|свидан|погуляем|отношен\w*\s+без\s+обязательств|"
    r"сдам|сниму|аренд|квартир|кондо|пхукет|байт(?:а|ов)?\b|"
    r"макияж|визаж|бров\w*|ресниц|ламинир|косметолог|мастер\s+маникюр|"
    r"ищу\s+модел|нужн\w*\s+модел|"
    r"курс\w*.{0,40}(?:qa|тестиров|программ|python|java)|"
    r"mate\s+academy|билет\s+в\s+it|импостер|"
    r"непросмотренн\w*\s+лектор|подписк\w*\s+на\s+совесть"
    r")"
)


def looks_like_not_a_lead(text: str) -> bool:
    """Чужой подряд / сторителлинг / опрос аудитории — не заявка."""
    raw = text or ""
    if NOT_A_LEAD_RE.search(raw):
        return True
    if OFFTOPIC_JUNK_RE.search(raw):
        return True
    return False


# DIY-чат / спор экспертов без найма подрядчика
DIY_SEO_CHAT_RE = re.compile(
    r"(?iu)(?:"
    r"поддомен\s+или\s+подпапк|подпапк\w*\s+или\s+поддомен|"
    r"robots(?:\.txt)?|sitemap|stimemap|каноникал|canonical|"
    r"мета[\s\-]?тег|title\s*/\s*description|схема\s+для\s+сайта"
    r")"
)

# Самопиар агентства / студии
AGENCY_PROMO_RE = re.compile(
    r"(?iu)(?:"
    r"мы\s*[—\-–]\s*[«\"]?\w|"
    r"с\s+\d{4}\s+года\s+(?:делаем|занимаемся|продвигаем)|"
    r"наше?\s+агентств|"
    r"делаем\s+сайты.{0,80}продвигаем|"
    r"продвигаем\s+seo\s+и\s+запускаем|"
    r"за\s+\d+\s+лет\s+поняли"
    r")"
)

# Реклама товара / склада / доставки — не SEO-лид
PRODUCT_AD_RE = re.compile(
    r"(?iu)(?:"
    r"актуальн\w*\s+наличие|наличие\s+[—\-–]\s*на\s+сайте|"
    r"гарантия\s+на\s+товар|собственный\s+склад|склад\s+в\s+\w+|"
    r"доставка\s+по\s+(?:евро|ес|украин|росси|мир)|"
    r"выбрат[ьи]\s+и\s+заказать|заказать\s*:\s*\w+\.\w+|"
    r"ноутбук\w*|макбук|macbook|\blaptop\b|айфон|iphone|"
    r"mixprice|adenix|\+\s*420\b|"
    r"евросоюз|чехи\w*\s*[—\-–].*склад"
    r")"
)

# Автор продаёт услуги / зовёт на разбор (не покупатель)
SELLER_PITCH_RE = re.compile(
    r"(?iu)(?:"
    r"пишите\s+разбор|пиши\s+разбор|оставьте?\s+заявк|"
    r"уважаемые\s+клиенты|"
    r"посмотри\w*\s+ваш\s+проект|разберу\s+ваш|"
    # исполнитель в комментариях: «посмотрела ваш сайт… могу взять проект»
    r"посмотрел\w*\s+ваш\s+(?:сайт|проект|лендинг)|"
    r"(?:могу|готов\w*)\s+взять\s+(?:проект|ваш|в\s+работу)|"
    r"(?<![A-Za-zА-Яа-яЁё0-9_])я\s+занимаюсь\s+"
    r"(?:комплексн|сео|seo|продвижен|оптимизац)|"
    r"продвигаю\s+бренд|привожу\s+клиент|"
    r"предлагаю\s+(?:услуги|аудит|разбор|сопровожден)|"
    r"записывайтесь|бесплатн\w*\s+разбор|"
    # «я» только отдельным словом: иначе ловит «рекомендациЯ SEO-специалиста»
    r"(?<![A-Za-zА-Яа-яЁё0-9_])я\s+(?:сеошник|seo[\s\-]?специалист|веду\s+сео)|"
    r"мо[её]\s+агентств|наш[ае]\s+агентств|"
    r"через\s+pinterest|в\s+pinterest|"
    r"\bpinterest\b|"
    r"сео\s+оптимизаци\w*\s+(?:пинтерест|pinterest|инст)|"
    r"без\s+систем\w*.*сео\s+оптимизац|"
    r"если\s+нужен\s+(?:грамотный\s+)?подрядчик|"
    r"потратите?\s+много\s+денег\s+на\s+(?:сео|seo)|"
    r"все\s+кто\s+(?:говорит|обещает).{0,40}(?:топ|сео|seo)|"
    r"работайте\s+лучше\s+через|"
    r"помогаю\s+бизнес\w*.{0,40}не\s+сливать|"
    r"не\s+сливать\s+бюджет|"
    r"готов\s+к\s+диалогу|открыт\s+к\s+диалогу|"
    r"готов\s+(?:взять|вести|помочь)|напишите\s+мне\s+в\s+(?:лс|директ)|"
    r"обращайтесь|в\s+лс\s+кто\s+хочет|"
    r"не\s+забудьте\s+зарег|"
    r"если\s+вам\s+отдают\s+сайт|"
    # лекция/гайд, а не владелец сайта, который сам упомянул robots/sitemap
    r"схема\s+для\s+сайта.*robots|"
    r"(?:чек[\s\-]?лист|гайд|инструкци\w*|шпаргалк\w*)\s.{0,60}(?:robots|sitemap)|"
    # экспертный контент / прогрев, не заявка
    r"пока\s+ты\s+думаешь|твои\s+конкуренты\s+уже|"
    r"забирают\s+твоих\s+клиент|"
    r"seo\s*[—\-–]\s*это\s+не|"
    r"поэтому\s+seo|"
    r"пока\s+ты\s+откладываешь|"
    r"нужно\s+ли\s+тебе\s+seo|"
    r"борьба\s+за\s+(?:уже\s+)?существующ|"
    # бесплатная консультация / «подарок» — не лид
    r"бесплатн\w*\s+консультац|"
    r"консультаци\w*\s+бесплатн|"
    r"бесплатн\w*\s+(?:разбор|аудит|созвон|созвон|звонок|сопровожден)|"
    r"дар\w*\s+консультац|"
    r"бесплатно\s+(?:помогу|посмотрю|разберу|проконсультир)|"
    r"проконсультирую\s+бесплатн|"
    r"первый\s+(?:созвон|разбор)\s+бесплатн"
    r")"
)

# SMM/соцсети под видом «сео»
SOCIAL_SEO_FAKE_RE = re.compile(
    r"(?iu)(?:"
    r"pinterest|пинтерест|"
    r"сео\s+оптимизаци\w*\s+(?:для\s+)?(?:пин|inst|рилс|tiktok)|"
    r"ключев\w+\s+фактор\w*\s+.*pinterest|"
    r"контент\s+.*pinterest.*купит"
    r")"
)


def _w(word: str) -> str:
    return rf"(?<![A-Za-zА-Яа-яЁё0-9_]){word}(?![A-Za-zА-Яа-яЁё0-9_])"


# Только сигнал ПОКУПАТЕЛЯ (не «нужно ли тебе SEO» и не лекции)
BUYER_INTENT_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        # Эталон: «ищу seo-шника… передавать заказы»
        r"ищу\s+(?:хорошего\s+)?(?:сео|seo|сэо)[\s\-]?шник",
        r"ищу\s+(?:сео|seo|сэо|сеошник|seo[\s\-]?специалист|подрядчик|специалист|агентств|фриланс|человека|аудит)",
        r"ищем\s+(?:сео|seo|сеошник|подрядчик|специалист|агентств)",
        r"ишем\s+(?:сео|seo|сеошник|подрядчик|специалист|агентств)",
        r"ишу\s+(?:сео|seo|сеошник|подрядчик)",
        # только с объектом найма — голые «ищу/нужен» ловят рефералы и блоги
        r"нужен\s+(?:сео|seo|сэо|сеошник|подрядчик|специалист|аудит|человек|сайт)",
        r"нужна\s+(?:помощь|семантика|сео|seo|разработк\w*\s+сайт)",
        r"нужно\s+(?:сео|seo|продвин|аудит|сделать\s+сайт)",
        r"нужны\s+(?:сео|seo|подрядчик|лиды|заявки)",
        # «нужна и реклама и сео-оптимизация» — слова между «нужно» и «сео»
        r"нуж(?:ен|на|но|ны)\s+(?:\S+\s+){1,3}(?:сео|seo|сеошник|продвижени|оптимизаци)",
        r"требуется\s+(?:сео|seo|подрядчик|специалист)",
        # «подскажите» только рядом с наймом/SEO, не голый DIY-чат
        # «поделитесь/скиньте/киньте сеошника» — та же просьба, другими словами
        r"(?:подскажите|посоветуйте|порекомендуйте|подел\w*|скинь\w*|подкинь\w*|кинь\w*)"
        r".{0,48}(?:сео|seo|сеошник|подрядчик|специалист|аудит)",
        r"(?:сео|seo|сеошник|подрядчик|специалист)"
        r".{0,48}(?:посоветуйте|порекомендуйте|подел\w*|скинь\w*|подкинь\w*)",
        # просьба о рекомендации/мнении подрядчика — тоже покупатель
        r"(?:нужн[оаы]|надо|требуется)\s+(?:мнение|совет|рекомендаци\w*|консультаци\w*)",
        r"ищу\s+(?:рекомендаци\w*|совет\w*|контакт\w*)",
        r"рекомендаци\w*\s+(?:сео|seo|сэо)[\s\-]?(?:специалист|шник|агентств)",
        r"(?:сео|seo|сеошник|подрядчик).{0,48}(?:подскажите|посоветуйте|порекомендуйте)",
        r"кто\s+(?:может|делает|займётся|займется|занимается|ведёт|ведет|делал)\s*.{0,12}(?:сео|seo|сайт|органик)",
        r"кто\s+(?:нибуд[ьи]|нибудь)\s+(?:может|делает|займ).{0,24}(?:сео|seo|сайт)",
        r"заказать\s+(?:сео|seo|аудит|продвижен)",
        r"ищу\s+подрядчик",
        # «нужен подрядчик» только про SEO/сайт, не любой чужой подряд
        r"нужен\s+подрядчик\w*.{0,30}(?:сео|seo|сайт|органик|продвижен)",
        r"(?:сео|seo|сайт).{0,30}нужен\s+подрядчик",
        r"помогите\s+(?:с\s+)?(?:сео|seo|сайт|органик|трафик|продвижен)",
        r"сколько\s+стоит\s+(?:сео|seo|продвижен|аудит)",
        r"looking\s+for\s+(?:an?\s+)?seo",
        r"need(?:s|ed)?\s+(?:an?\s+)?seo",
        r"hire\s+(?:an?\s+)?seo",
        r"вывести\s+(?:сайт\s+)?в\s+топ",
        r"продвинуть\s+сайт",
        r"раскрутить\s+сайт",
        r"органика\s+упал",
        r"трафик\s+(?:с\s+поиска\s+)?упал",
        r"позици\w*\s+(?:упал|просел)",
        r"упал[аи]?\s+(?:органик|трафик|позици)",
        r"просел[аи]?\s+(?:органик|трафик|позици)",
        r"нет\s+трафика|мало\s+трафика",
        # «место в поиске 16, летом было 20», «вкладываю в продвижение, а роста нет»
        r"мест\w*\s+в\s+(?:поиске|выдаче)\s*[№#]?\s*\d",
        r"мест\w*\s+в\s+выдаче",
        r"(?:вкладыва\w*|трач\w*|влива\w*)\s+.{0,30}(?:на|в)\s+продвижени",
        r"не\s+(?:видно|находят|видят|вижу)\s+.{0,15}в\s+(?:поиске|выдаче|google|яндекс)",
        r"нет\s+заявок|мало\s+заявок",
        r"выпал\w*\s+из\s+(?:поиска|индекса)",
        r"агентство\s+кинуло|ищу\s+другого\s+сео",
        r"видимость\s+в\s+(?:chatgpt|ии|нейросет|алис)",
        r"попасть\s+в\s+ответы",
        r"киньте\s+(?:сео|seo|сеошник|контакт)",
        r"сеошник\s+в\s+лс|сео\s+в\s+лс",
        r"ведение\s+сео|вести\s+сео",
        r"сопровождение\s+(?:сео|сайта|переезда)",
        # white-label / партнёр под заказы клиентов
        r"передавать\s+заказ",
        r"передать\s+заказ",
        r"специалист\w*\s+с\s+кейсам",
        r"white[\s\-]?label|субисполнител|субподряд",
    )
]

# совместимость со старым именем
INTENT_PATTERNS = BUYER_INTENT_PATTERNS

# SEO/GEO маркеры
SEO_MARKERS = [
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        _w(r"seo"),
        _w(r"сео"),
        _w(r"geo"),
        _w(r"гео"),
        r"сеошник\w*",
        r"сеопродвижен\w*",
        r"сео[\s\-]?продвижен",
        r"сео[\s\-]?специалист",
        r"поисков\w+\s+(?:трафик|оптимизац|выдач)",
        r"органик\w+\s+трафик",
        r"органик[аиуе]",
        r"вывести\s+(?:сайт\s+)?в\s+топ",
        r"продвинут[ьи]\s+сайт",
        r"продвижени[еюя]\s+сайт",
        r"продвижени[еюя]\s+(?:в\s+)?(?:яндекс|google|поиске)",
        r"раскрутк\w*\s+сайт",
        r"оптимизаци\w*\s+сайт",
        r"ключев\w+\s+слов",
        r"семантик\w*",
        r"яндекс(?:\s+директ)?|google\s+(?:ads|search)|вебмастер|search\s+console",
        r"аудит\s+сайт",
        r"сео[\s\-]?аудит",
        r"chatgpt|perplexity|ai\s+overview",
        r"видимость\s+в\s+(?:поиске|выдаче|ии|chatgpt|нейросет)",
        r"позици\w*\s+в\s+(?:поиске|выдаче|яндекс|google)",
        r"мест\w*\s+в\s+(?:поиске|выдаче)\s*[№#]?\s*\d",
        r"мест\w*\s+в\s+выдаче",
        r"не\s+(?:видно|находят|видят|вижу)\s+.{0,15}в\s+(?:поиске|выдаче|google|яндекс)",
        r"ранжир\w*",
        r"выдач[аеиу]",
        r"из\s+поиска",
        r"в\s+топе",
        r"топ(?:\s*[-–]?\s*|\s+)(?:10|20|яндекс|google)",
    )
]

SITE_PAIN_RE = re.compile(
    r"(?iu)сайт|лендинг|интернет[\s\-]?магазин|карточк\w*\s+товар|"
    r"яндекс|google|органик|выдач|позици|трафик|индекс|"
    r"вебмастер|search\s+console|семантик|ключев"
)

# Instagram / Reels — SMM, но не режем, если явно про SEO/сайт
SMM_ONLY_RE = re.compile(
    r"(?iu)(?:"
    r"\binst(?:agram|a)?\b|\bинст(?:аграм(?:е|а|у)?)?\b|\bрилс\w*|\breels?\b|"
    r"\btiktok\b|\bтик[\s\-]?ток\b|"
    r"просмотр\w*\s+(?:в\s+)?(?:инст|рилс|stories|сторис)|"
    r"охват\w*\s+(?:в\s+)?(?:инст|рилс)|stories|сторис"
    r")"
)

# Custdev / бартер / «ищу маркетологов» — не SEO-лид
OFFTOPIC_NOT_SEO_RE = re.compile(
    r"(?iu)(?:"
    r"custdev|кастдев|касдэв|кастомер\s*дев|"
    r"ищу\s+маркетолог|ищем\s+маркетолог|нужен\s+маркетолог|"
    r"маркетолог\w*\s+на\s+(?:кас|каст|cust|интервью)|"
    r"на\s+интервью|глубинн\w*\s+интервью|"
    r"с\s+меня\s+аудит|обмен\s+услуг|бартер|"
    r"аудит\s+техник|техн(?:ичк|ическ)\w*\s+(?:аудит|сайт)|"
    r"ищу\s+(?:респондент|участник\w*\s+исследован)|"
    r"для\s+исследован\w*"
    r")"
)

# Явный найм SEO — можно слать без DeepSeek (модель часто режет зря)
CLEAR_HIRE_RE = re.compile(
    r"(?iu)(?:"
    r"ищу\s+(?:хорошего\s+|опытн\w*\s+)?(?:сео|seo|сэо)[\s\-]?шник|"
    r"ищу\s+(?:опытн\w*\s+)?(?:сео|seo|сэо)\s*специалист|"
    r"ищу\s+(?:опытн\w*\s+)?(?:сео|seo)\b|"
    r"ищу\s+(?:подрядчик|специалист|фрилансер).{0,30}(?:сео|seo)|"
    r"нужен\s+(?:сео|seo|сеошник)|"
    r"нужен\s+сайт.{0,60}(?:сео|seo|яндекс|сеопродвижен)|"
    r"нужен\s+(?:сео|seo)\s*специалист|"
    r"нужен\s+(?:подрядчик|специалист).{0,30}(?:сео|seo)|"
    r"нужн[оаы]\s+(?:мнение|рекомендаци\w*).{0,48}(?:сео|seo|сеошник)|"
    r"рекомендаци\w*\s+(?:сео|seo|сэо)[\s\-]?специалист|"
    r"посоветуйте\s+(?:сео|seo|сеошник|подрядчик\w*\s+по\s+сео)|"
    r"подскажите.{0,40}(?:сеошник|сео[\s\-]?специалист)|"
    r"заказать\s+(?:сео|seo)|"
    r"looking\s+for\s+(?:an?\s+)?seo|"
    r"need(?:s|ed)?\s+(?:an?\s+)?seo|"
    r"передавать\s+заказ\w*.{0,40}(?:сео|seo)|"
    r"(?:органика|позиции|трафик)\s+упал\w*.{0,60}(?:кто\s+может|помог|посмотр)"
    r")"
)


def looks_like_clear_hire(text: str) -> bool:
    raw = text or ""
    if looks_like_not_a_lead(raw) or looks_like_seller_pitch(raw):
        return False
    return bool(CLEAR_HIRE_RE.search(raw))


# Явная SEO-боль / заказ (без «ищу маркетолога»)
STRONG_SEO_NEED_RE = re.compile(
    r"(?iu)(?:"
    r"\bсео\b|\bseo\b|сеошник|сео[\s\-]?специалист|сео[\s\-]?аудит|"
    r"сеопродвижен\w*|сео[\s\-]?продвижен|"
    r"органик[аиуе]|позици\w*\s+(?:упал|просел|в\s+поиске|в\s+выдаче)|"
    r"вывести\s+(?:сайт\s+)?в\s+топ|продвин(?:уть|ени\w*)\s+сайт|"
    r"раскрутк\w*\s+сайт|поисков\w+\s+трафик|трафик\s+из\s+поиска|"
    r"яндекс\s+вебмастер|search\s+console|в\s+выдаче|"
    r"заказать\s+сео|нужен\s+сео|ищу\s+сео|сколько\s+стоит\s+сео|"
    r"подрядчик\w*\s+(?:по\s+)?сео|видимость\s+в\s+(?:chatgpt|ии|нейросет)"
    r")"
)


def strip_hashtags(text: str) -> str:
    """Убираем #теги — спам с #SEO без темы иначе проходит."""
    return re.sub(r"#[\wА-Яа-яЁё]+", " ", text or "")


def looks_like_product_ad(text: str) -> bool:
    return bool(PRODUCT_AD_RE.search(text or ""))


def looks_like_diy_seo_chat(text: str) -> bool:
    """Техспор/DIY без найма: «поддомен или подпапка?», robots/sitemap…"""
    raw = text or ""
    if not DIY_SEO_CHAT_RE.search(raw):
        return False
    # Явный найм — пропускаем (не DIY)
    if re.search(
        r"(?iu)ищу\s+(?:сео|seo|сеошник|подрядчик|специалист)|"
        r"нужен\s+(?:сео|seo|сеошник|подрядчик)|"
        r"заказать\s+(?:сео|seo)|передавать\s+заказ",
        raw,
    ):
        return False
    return True


def looks_like_agency_promo(text: str) -> bool:
    return bool(AGENCY_PROMO_RE.search(text or ""))


def looks_like_seller_pitch(text: str) -> bool:
    raw = text or ""
    # Покупатель «нужна рекомендация SEO-специалиста» — не продавец.
    if CLEAR_HIRE_RE.search(raw):
        return False
    if SELLER_PITCH_RE.search(raw) or SOCIAL_SEO_FAKE_RE.search(raw):
        return True
    # много телефонов / «пишите» без запроса подрядчика
    phones = len(re.findall(r"\+?\d[\d\s\-]{8,}\d", raw))
    if phones >= 1 and looks_like_product_ad(raw):
        return True
    return False


def looks_like_smm_not_seo(text: str) -> bool:
    """Чистый SMM без SEO/сайт-сигнала."""
    raw = text or ""
    if not SMM_ONLY_RE.search(raw):
        return False
    body = strip_hashtags(raw)
    if has_seo_marker(body) or SITE_PAIN_RE.search(body):
        return False
    return True


def looks_like_offtopic_not_seo(text: str) -> bool:
    """Custdev, бартер, товарка, донаты, питч продавца, DIY-чат, промо агентства."""
    raw = text or ""
    if OFFTOPIC_NOT_SEO_RE.search(raw):
        return True
    if looks_like_product_ad(raw):
        return True
    if looks_like_seller_pitch(raw):
        return True
    if looks_like_diy_seo_chat(raw):
        return True
    if looks_like_agency_promo(raw):
        return True
    return False


def has_search_pain(text: str) -> bool:
    return bool(
        re.search(
            r"(?iu)упал[аи]?|просел[аи]?|нет\s+трафика|мало\s+трафика|"
            r"выпал\w*\s+из|не\s+индексир|воздух|кинуло\s+агентств|"
            r"заявок\s+нет|жр[её]т\s+бюджет|нет\s+заявок|мало\s+заявок|"
            r"нужны?\s+(?:лиды|заявки|клиенты)|ищу\s+лид|"
            r"позиции\s+просел|трафик\s+пропал|органик\w*\s+нет|"
            r"мест\w*\s+в\s+(?:поиске|выдаче)\s*[№#]?\s*\d|мест\w*\s+в\s+выдаче|"
            # боль владельца сайта — дальше решает DeepSeek, не правила
            r"индексаци\w*\s+(?:черепаш|стоит|встала|не\s+идёт|медленн|проблем)|"
            r"(?:плохо|слабо)\s+индексир|нет\s+позици|"
            r"сайт\w*\s+(?:никто\s+)?не\s+(?:находят|видно|видят)",
            text or "",
        )
    )


def count_cyrillic(text: str) -> int:
    return len(CYR_RE.findall(text or ""))


def count_latin(text: str) -> int:
    return len(LAT_RE.findall(text or ""))


def looks_ukrainian(text: str) -> bool:
    raw = text or ""
    if UKR_LETTER_RE.search(raw):
        return True
    return bool(UKR_HINT_RE.search(raw))


def is_mostly_russian(text: str, *, min_cyr: int = 4, min_ratio: float = 0.22) -> bool:
    """Русский текст. Украинский и чистая латиница — нет."""
    raw = text or ""
    if looks_ukrainian(raw):
        return False
    cyr = count_cyrillic(raw)
    if cyr < min_cyr:
        return False
    if cyr >= 10:
        return True
    lat = count_latin(raw)
    if lat == 0:
        return True
    return (cyr / (cyr + lat)) >= min_ratio


def is_junk(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 3:
        return True
    if any(p.search(raw) for p in JUNK_PATTERNS):
        return True
    if looks_like_product_ad(raw):
        return True
    if len(re.findall(r"(?<!\d)\d{13,20}(?!\d)", raw)) >= 2:
        return True
    return False


def has_intent(text: str) -> bool:
    """Есть сигнал покупателя (ищу/нужен подрядчика…), не лекция."""
    raw = text or ""
    if looks_like_seller_pitch(raw) or looks_like_not_a_lead(raw):
        return False
    return any(p.search(raw) for p in BUYER_INTENT_PATTERNS)


def has_seo_marker(text: str) -> bool:
    return any(p.search(text or "") for p in SEO_MARKERS)


def looks_like_seo_contractor_request(text: str, tag: str = "") -> bool:
    """
    Лид = автор сам ищет SEO/GEO подрядчика или просит помочь с СВОИМ сайтом.
    """
    _ = tag
    raw = text or ""
    if not is_mostly_russian(raw):
        return False
    if is_junk(raw):
        return False
    if looks_like_not_a_lead(raw):
        return False
    if looks_like_offtopic_not_seo(raw):
        return False
    if looks_like_smm_not_seo(raw):
        return False
    if looks_like_seller_pitch(raw):
        return False

    body = strip_hashtags(raw)
    seo = has_seo_marker(body)
    strong = bool(STRONG_SEO_NEED_RE.search(body))
    buyer = has_intent(raw)
    pain = has_search_pain(raw)

    # Нужен явный найм ИЛИ боль по своему трафику/позициям + SEO-маркер в тексте
    if not buyer and not pain:
        return False
    if not (strong or seo):
        return False
    if pain and (strong or seo):
        return True
    if buyer and (strong or seo):
        return True
    return False


def looks_like_topic_lead(text: str, tag: str) -> bool:
    """Префильтр под хэштег: SEO отдельно, иначе интент + тема тега."""
    raw = text or ""
    tag_n = normalize_hashtag(tag).casefold()
    if not tag_n:
        return False
    if not is_mostly_russian(raw):
        return False
    if looks_like_not_a_lead(raw):
        return False
    if tag_n in {"seo", "сео"}:
        return looks_like_seo_contractor_request(raw, tag)
    if tag_n in {"geo", "гео"}:
        if looks_like_offtopic_not_seo(raw) or looks_like_smm_not_seo(raw):
            return False
        # GEO: только реальный найм / боль — не блог «гоняюсь за AI-видимостью»
        if looks_like_seo_contractor_request(raw, tag):
            return True
        geo_topic = bool(
            re.search(
                r"(?iu)chatgpt|perplexity|нейросет|ai\s*overview|"
                r"видимость\s+в\s+(?:ии|chatgpt|нейросет)|"
                r"гео\s+продвижен|geo[\s\-]?оптимиз|ответы\s+ии|"
                r"ai[\s\-]?видимост",
                raw,
            )
        )
        if not geo_topic:
            return False
        # Только найм / просьба помочь — не «что пробовали?»
        return has_intent(raw) and (
            has_seo_marker(strip_hashtags(raw)) or bool(STRONG_SEO_NEED_RE.search(raw))
        )
    if not has_intent(raw):
        return False
    if text_mentions_tag(raw, tag):
        return True
    if has_seo_marker(raw) and tag_n not in {"smm", "смм", "marketing", "маркетинг"}:
        if re.search(r"(?iu)\bсео\b|\bseo\b|сеошник|органик|яндекс|аудит\s+сайт", raw):
            return False
    return bool(
        re.search(
            r"(?iu)ищу|нужен|нужна|посоветуйте|подскажите|стоимость|подрядчик|"
            r"специалист|фриланс|агентств|заказ",
            raw,
        )
    )


def scrub_post_text(text: str) -> str:
    """Чистка текста от служебного мусора Threads."""
    body = clean_post_text(text or "")
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.casefold() in {"translate", "перевести", "see translation"}:
            continue
        lines.append(s)
    return "\n".join(lines).strip()


# Слабый намёк на спрос — пускаем в DeepSeek (Recent #SEO почти без «ищу» в тексте)
WEAK_LEAD_HINT_RE = re.compile(
    r"(?iu)(?:"
    r"ищу|ишем|ишу|нужен|нужна|нужно|нужны|требуется|"
    r"подскаж|посовет|порекоменд|киньте|хелп|help|"
    r"кто\s+(?:может|делает|ведёт|ведет|займ|занимается|нибуд)|"
    r"помог\w*|заказать|сколько\s+стоит|"
    r"упал|просел|выпал|мало\s+трафик|нет\s+заявок|нет\s+трафик|"
    r"подрядчик|сеошник|фриланс|агентств|"
    r"бюджет|заявк|лиды|клиент\w*\s+с\s+сайт|"
    r"продвин\w*\s+сайт|вывести\s+(?:в\s+)?топ|раскрут|"
    r"органик|позици|индекс|вебмастер|"
    r"chatgpt|perplexity|нейросет|geo|гео|"
    r"передавать\s+заказ|свой\s+сайт|наш\s+сайт|интернет[\s\-]?магазин"
    r")"
)


def looks_like_weak_seo_candidate(text: str, tag: str = "") -> bool:
    """Русский пост про SEO/сайт с намёком на спрос — решение за DeepSeek."""
    raw = text or ""
    if looks_like_seller_pitch(raw) or looks_like_not_a_lead(raw):
        return False
    if looks_like_agency_promo(raw) or looks_like_diy_seo_chat(raw):
        return False
    if not WEAK_LEAD_HINT_RE.search(raw):
        return False
    body = strip_hashtags(raw)
    tag_n = normalize_hashtag(tag).casefold()
    site_buyer = bool(
        re.search(
            r"(?iu)онлайн[\s\-]?магазин|интернет[\s\-]?магазин|"
            r"сайт.{0,24}под\s+ключ|под\s+ключ.{0,24}сайт|"
            r"хочу\s+(?:создать|сделать).{0,24}(?:сайт|магазин)|"
            r"нужен\s+сайт",
            raw,
        )
    )
    seo_in_text = (
        has_seo_marker(body)
        or has_seo_marker(raw)
        or bool(STRONG_SEO_NEED_RE.search(raw))
    )
    # Лента #сео: «магазин под ключ» без слова SEO в тексте — всё равно в теме
    if not seo_in_text and not (tag_n in {"seo", "сео"} and site_buyer):
        return False
    if not (
        has_intent(raw)
        or has_search_pain(raw)
        or looks_like_clear_hire(raw)
        or site_buyer
    ):
        return False
    return True


def post_passes_filters(
    text: str,
    tag: str,
    *,
    require_russian: bool = True,
    require_seo_intent: bool = True,
) -> tuple[bool, str]:
    """
    (ok, reason). Жёстко режем мусор/продавцов; спорные — в DeepSeek.
    """
    raw = text or ""
    body = scrub_post_text(raw)
    check_text = body if body else raw

    if is_junk(check_text):
        return False, "мусор/спам"
    if looks_like_product_ad(check_text):
        return False, "реклама товара — не SEO"
    if looks_like_seller_pitch(check_text):
        return False, "автор продаёт услуги / бесплатная консультация"
    if looks_like_agency_promo(check_text):
        return False, "самопиар агентства"
    if looks_like_diy_seo_chat(check_text):
        return False, "DIY/техспор — не заказ"
    if looks_like_not_a_lead(check_text):
        return False, "не заявка (реферал/блог/опрос)"

    # Явный «ищу сеошника» — сразу ок (не тонем в «нет запроса по теме»)
    if looks_like_clear_hire(check_text) or looks_like_clear_hire(raw):
        if require_russian:
            if looks_ukrainian(check_text) or looks_ukrainian(raw):
                return False, "украинский — нужен русский"
            if not (is_mostly_russian(check_text) or is_mostly_russian(raw)):
                return False, f"не русский текст (кириллицы: {count_cyrillic(raw)})"
        return True, ""

    keyword_hit = False
    try:
        from search_phrases import text_hits_keyword

        keyword_hit, _which = text_hits_keyword(check_text)
    except Exception:
        keyword_hit = False

    if require_russian:
        if looks_ukrainian(check_text) or looks_ukrainian(raw):
            return False, "украинский — нужен русский"
        if not (is_mostly_russian(check_text) or is_mostly_russian(raw)):
            return False, f"не русский текст (кириллицы: {count_cyrillic(raw)})"

    tag_n = normalize_hashtag(tag).casefold()
    if require_seo_intent or tag_n in {"seo", "сео", "geo", "гео"}:
        if tag_n in {"seo", "сео", "geo", "гео"} and looks_like_offtopic_not_seo(
            check_text
        ):
            return False, "оффтоп / не покупатель SEO"
        if tag_n in {"seo", "сео", "geo", "гео"} and looks_like_smm_not_seo(check_text):
            return False, "SMM/Instagram — не SEO"
        topic_ok = looks_like_topic_lead(check_text, tag) or looks_like_topic_lead(
            raw, tag
        )
        soft_ok = False
        if keyword_hit and not looks_like_seller_pitch(check_text):
            body_soft = strip_hashtags(check_text)
            seo_in_text = has_seo_marker(body_soft) or bool(
                STRONG_SEO_NEED_RE.search(body_soft)
            )
            if seo_in_text and (has_intent(check_text) or has_search_pain(check_text)):
                soft_ok = True
        weak_ok = looks_like_weak_seo_candidate(check_text, tag) or looks_like_weak_seo_candidate(
            raw, tag
        )
        if not (topic_ok or soft_ok or weak_ok):
            return False, f"нет запроса по теме #{normalize_hashtag(tag) or tag}"

    return True, ""
