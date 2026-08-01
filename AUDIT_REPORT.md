# گزارش ممیزی ربات روبیکا Atomic Shop

تاریخ ممیزی: 2026-08-01

## محدوده و معماری

این مخزن فقط نسخه روبیکا است. نقطه ورود `main.py`، routing در `router.py`،
دیتابیس مستقل PostgreSQL در `atomic_rubika_db` و پورت محلی
`127.0.0.1:8081` است. توکن روبیکا، مدیران، دیتابیس، درگاه، پرداخت‌ها، سفارش‌ها
و deployment آن از تلگرام کاملاً جدا هستند.

جریان خرید: update روبیکا → کنترل event تکراری و session → اعتبارسنجی و ایجاد
سفارش → انتخاب دقیقاً یک روش پرداخت → finalize اتمیک → fulfillment claim →
ارسال یک‌باره G2Bulk → reconciliation → outbox اعلان مستقل کاربر و مدیر.

## باگ‌های پیدا و رفع‌شده

| شدت | علت اصلی | اصلاح |
|---|---|---|
| بحرانی | callback تکراری قبل از تشخیص idempotency وضعیت سفارش را رد می‌کرد | payment ابتدا قفل/بازخوانی می‌شود و callback تأییدشده با همان ref no-op است |
| بحرانی | کاربر می‌توانست هنگام وجود authority یا رسید pending روش پرداخت را عوض کند | guard اتمیک برای gateway/card/wallet و lock order→payment اضافه شد |
| بحرانی | ترتیب lockها در callback و review مستعد deadlock بود | ترتیب lock همه مسیرهای مالی یکسان شد |
| شدید | سفارش دارای پرداخت در جریان ممکن بود منقضی شود | expiry، authority/receipt فعال را مستثنی می‌کند؛ not-paid قطعی پس از reconciliation رد می‌شود |
| شدید | پاسخ مبهم G2Bulk امکان برداشت/ارسال دوباره داشت | idempotency key، claim، exact remark recovery و منع retry مبهم اضافه شد |
| شدید | اعلان نتیجه تحویل ممکن بود گم شود | outbox پایدار کاربر/مدیر و retry worker استفاده می‌شود |
| متوسط | هزینه تأمین‌کننده با float محاسبه می‌شد | `Decimal` و rounding مشخص اضافه شد |
| متوسط | callback تأییدشده تکراری false failure ایجاد می‌کرد | finalize idempotent همراه کنترل authority/ref شد |
| متوسط | ترتیب محصولات ضمنی بود | `sort_order`، index و فرمان‌های move مدیر اضافه شد |
| متوسط | تاریخچه وضعیت سفارش وجود نداشت | `order_status_history` و trigger اضافه شد |
| کم | داده کارت واقعی در تست قالب‌بندی قرار داشت | با داده تست ساختگی جایگزین شد |

## دیتابیس و کنترل مالی

- CHECK برای قیمت/موجودی/مبالغ و foreign keyهای user/order/product برقرار است.
- authority، ref، ledger reference، payment link و fulfillment idempotency یکتا هستند.
- wallet debit/credit با `FOR UPDATE` و ledger در یک transaction انجام می‌شود.
- پرداخت gateway تکراری دوباره کیف پول را شارژ یا سفارش را paid نمی‌کند.
- رسید کارت‌به‌کارت قبل از تأیید دوم مدیر اثری مالی ندارد.
- محصول غیرفعال/ناموجود یا قیمتی که حین خرید تغییر کرده قابل خرید نیست.

## دسترسی، امنیت و عملکرد

- فقط `RUBIKA_ADMIN_ID` مالک اصلی است؛ افزودن/حذف مدیران دیگر owner-only است.
- پشتیبانی برای کاربران در منوی عمومی وجود دارد، اما پنل/رسیدها/لیست تیکت‌های
  مدیریتی فقط پس از `is_admin` اجرا می‌شوند.
- `.env` در Git نیست؛ مقدار واقعی token، merchant یا API key در HEAD وجود ندارد.
- webhook secret، event deduplication، input limits و amount bounds بررسی شدند.
- updateهای هر chat به‌ترتیب و chatهای متفاوت concurrent پردازش می‌شوند؛ HTTP
  sessionها reuse و bounded هستند.
- Docker DB را منتشر نمی‌کند و bot روی loopback، read-only، healthcheck و log
  rotation اجرا می‌شود.

## برابری قابلیت با تلگرام

کاتالوگ 14 محصول و قیمت‌ها برابر است؛ جم و بسته هفتگی/ماهانه صفحه اول و شش Level
Up صفحه دوم است. پرداخت درگاه لینک مستقیم دارد و متن خاموش‌کردن VPN ندارد. شماره
کارت به‌صورت پیام مستقل قابل کپی است. این برابری هیچ اتصال runtime یا دیتابیس
مشترکی ایجاد نمی‌کند.

## نتیجه تست‌ها

| بخش | وضعیت | مدرک |
|---|---|---|
| Unit/Regression روبیکا | Passed | 44 تست محلی |
| Ruff | Passed | بدون خطای lint |
| Syntax همه فایل‌های Python | Passed | compile روی 12 فایل |
| وابستگی‌های نصب‌شده | Passed | `pip check` بدون dependency شکسته |
| payment/card/wallet با mock | Passed | ownership، lock، duplicate callback و method collision |
| G2Bulk با mock | Passed | ambiguous response، exact recovery، status و outbox |
| RBAC | Passed | مدیر فرعی نمی‌تواند مدیران را مدیریت کند؛ owner می‌تواند |
| ترتیب/صفحه‌بندی | Passed | محصولات اصلی صفحه اول و Level Up صفحه دوم |
| concurrency درون‌پردازشی | Passed | ordering هر chat و concurrency بین chatها |
| تراکنش واقعی زرین‌پال/G2Bulk | Blocked | عمداً برای جلوگیری از هزینه واقعی اجرا نشد |
| migration و restore روی PostgreSQL واقعی | Blocked محلی | Docker Desktop خاموش؛ روی VPS اجرا شود |
| load test تولیدی | Blocked | staging مستقل در دسترس نبود |

## بکاپ و بازیابی

`scripts/backup.sh` بکاپ custom-format مستقل روبیکا می‌سازد.
`scripts/restore-drill.sh` فقط روی دیتابیس موقت restore و سپس cleanup می‌کند.

## ریسک‌های باقی‌مانده

- API واقعی روبیکا، DNS/TLS، زرین‌پال و G2Bulk فقط در VPS/staging قابل تأیید است.
- پس از سه پاسخ قطعی not-paid، پرداخت رد می‌شود؛ late payment خارج از TTL باید از
  گزارش تطبیق مالی بررسی شود.
- تضمین مطلق «بدون باگ» ممکن نیست؛ انتشار منوط به backup، restore drill، تست
  sandbox و پایش لاگ است.
