CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  rubika_id TEXT UNIQUE NOT NULL,
  chat_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
  referred_by BIGINT REFERENCES users(id),
  card_number TEXT NOT NULL DEFAULT '',
  card_verified BOOLEAN NOT NULL DEFAULT false,
  blocked BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS admins (
  rubika_id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'admin',
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS categories (
  id BIGSERIAL PRIMARY KEY,
  title TEXT UNIQUE NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS products (
  id BIGSERIAL PRIMARY KEY,
  category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK (kind IN ('gem','sense_mobile','sense_pc','store')),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  amount INTEGER,
  supplier_sku TEXT NOT NULL DEFAULT '',
  supplier_cost_usd NUMERIC(18,6),
  price BIGINT NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
  active BOOLEAN NOT NULL DEFAULT true,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE products
  ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_products_catalogue_order
  ON products(kind,active,sort_order,id);
CREATE INDEX IF NOT EXISTS idx_categories_catalogue_order
  ON categories(active,sort_order,id);

CREATE TABLE IF NOT EXISTS promo_codes (
  id BIGSERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  code_type TEXT NOT NULL CHECK (code_type IN ('gift','discount')),
  value BIGINT NOT NULL CHECK (value > 0),
  max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses > 0),
  used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
  active BOOLEAN NOT NULL DEFAULT true,
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
  id BIGSERIAL PRIMARY KEY,
  code_id BIGINT NOT NULL REFERENCES promo_codes(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  applied_order_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(code_id,user_id)
);
CREATE TABLE IF NOT EXISTS pending_discounts (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  code_id BIGINT NOT NULL REFERENCES promo_codes(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending',
  total_amount BIGINT NOT NULL CHECK (total_amount > 0),
  discount_amount BIGINT NOT NULL DEFAULT 0 CHECK (discount_amount >= 0),
  wallet_paid BIGINT NOT NULL DEFAULT 0 CHECK (wallet_paid >= 0),
  payable_amount BIGINT NOT NULL CHECK (payable_amount >= 0),
  payment_method TEXT,
  player_id TEXT NOT NULL DEFAULT '',
  player_name TEXT NOT NULL DEFAULT '',
  promo_code TEXT NOT NULL DEFAULT '',
  paid_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE orders
  ADD COLUMN IF NOT EXISTS inventory_reserved BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS order_status_history (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  old_status TEXT,
  new_status TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION record_order_status_transition()
RETURNS trigger AS $$
BEGIN
  IF TG_OP='INSERT' THEN
    INSERT INTO order_status_history(order_id,old_status,new_status)
    VALUES(NEW.id,NULL,NEW.status);
  ELSIF OLD.status IS DISTINCT FROM NEW.status THEN
    INSERT INTO order_status_history(order_id,old_status,new_status)
    VALUES(NEW.id,OLD.status,NEW.status);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_order_status_transition ON orders;
CREATE TRIGGER trg_order_status_transition
AFTER INSERT OR UPDATE OF status ON orders
FOR EACH ROW EXECUTE FUNCTION record_order_status_transition();
CREATE TABLE IF NOT EXISTS order_items (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  product_id BIGINT NOT NULL REFERENCES products(id),
  title TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_price BIGINT NOT NULL CHECK (unit_price > 0)
);
CREATE TABLE IF NOT EXISTS payments (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT REFERENCES orders(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  purpose TEXT NOT NULL CHECK (purpose IN ('order','wallet')),
  provider TEXT NOT NULL,
  amount BIGINT NOT NULL CHECK (amount > 0),
  authority TEXT UNIQUE,
  ref_id TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  expires_at TIMESTAMPTZ NOT NULL,
  verified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS verify_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS wallet_ledger (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  amount BIGINT NOT NULL CHECK (amount <> 0),
  entry_type TEXT NOT NULL,
  reference TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS receipts (
  id BIGSERIAL PRIMARY KEY,
  payment_id BIGINT UNIQUE NOT NULL REFERENCES payments(id),
  user_id BIGINT NOT NULL REFERENCES users(id),
  source_chat_id TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  file_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  reviewed_by TEXT,
  reviewed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tickets (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  department TEXT NOT NULL DEFAULT 'عمومی',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ticket_messages (
  id BIGSERIAL PRIMARY KEY,
  ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  sender_type TEXT NOT NULL CHECK (sender_type IN ('user','admin')),
  sender_id TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS departments (
  id BIGSERIAL PRIMARY KEY,
  title TEXT UNIQUE NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS sessions (
  rubika_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT '',
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS forced_channels (
  id BIGSERIAL PRIMARY KEY,
  chat_id TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  invite_url TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS join_requests (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending',
  reviewed_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_join_request_pending
  ON join_requests(user_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS profit_snapshots (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id),
  sale_toman BIGINT NOT NULL,
  supplier_cost_usd NUMERIC(18,6),
  usd_toman_rate BIGINT,
  supplier_cost_toman BIGINT,
  gross_profit_toman BIGINT,
  fx_source TEXT NOT NULL DEFAULT '',
  captured_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fulfillments (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id),
  provider TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  provider_order_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE fulfillments
  ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE fulfillments
  ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
ALTER TABLE fulfillments
  ADD COLUMN IF NOT EXISTS user_notified_at TIMESTAMPTZ;
ALTER TABLE fulfillments
  ADD COLUMN IF NOT EXISTS admin_notified_at TIMESTAMPTZ;
DO $notification_outbox$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings
    WHERE key='fulfillment_notification_outbox_v1'
  ) THEN
    -- Existing terminal rows predate the durable outbox and may already have
    -- been announced. Backfill them to avoid replaying old notifications.
    UPDATE fulfillments
    SET user_notified_at=COALESCE(user_notified_at,now()),
        admin_notified_at=COALESCE(admin_notified_at,now())
    WHERE status IN ('COMPLETED','FAILED');

    INSERT INTO settings(key,value)
    VALUES('fulfillment_notification_outbox_v1','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $notification_outbox$;
CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGSERIAL PRIMARY KEY,
  admin_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  details TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_pending ON receipts(created_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_tickets_open ON tickets(updated_at DESC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_processed_events_created ON processed_events(created_at);

INSERT INTO settings(key,value) VALUES
 ('shop_name','اتومیک شاپ روبیکا'),
 ('welcome_text','✨ به اتومیک شاپ روبیکا خوش اومدی! ✨'),
 ('help_text',''),
 ('support_prompt',''),
 ('support_id','@omid_1797'),
 ('sales_enabled','1'),
 ('payments_enabled','1'),
 ('zarinpal_enabled','1'),
 ('zarinpal_merchant_id',''),
 ('gem_profit_percent','10'),
 ('card_enabled','1'),
 ('card_number',''),
 ('card_holder',''),
 ('card_bank',''),
 ('usd_toman_rate','')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings WHERE key='inventory_reservation_v1'
  ) THEN
    WITH reserved AS (
      SELECT i.product_id,sum(i.quantity)::integer quantity
      FROM order_items i JOIN orders o ON o.id=i.order_id
      WHERE o.status='pending' AND NOT o.inventory_reserved
      GROUP BY i.product_id
    )
    UPDATE products p
    SET stock=greatest(0,p.stock-r.quantity)
    FROM reserved r WHERE p.id=r.product_id;

    UPDATE orders SET inventory_reserved=true
    WHERE status='pending' AND NOT inventory_reserved;

    INSERT INTO settings(key,value)
    VALUES('inventory_reservation_v1','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $$;

INSERT INTO departments(title) VALUES ('عمومی'),('مالی'),('پیگیری سفارش')
ON CONFLICT (title) DO NOTHING;
INSERT INTO categories(title) VALUES ('جم فری‌فایر'),('سنسیویتی موبایل'),('سنسیویتی PC')
ON CONFLICT DO NOTHING;
INSERT INTO products(kind,title,amount,supplier_sku,price,stock)
SELECT * FROM (VALUES
 ('gem','🎯 لول‌آپ سطح 6',6,'Level Up Package - Level 6',65000,9999),
 ('gem','🎯 لول‌آپ سطح 10',10,'Level Up Package - Level 10',110000,9999),
 ('gem','🎯 لول‌آپ سطح 15',15,'Level Up Package - Level 15',110000,9999),
 ('gem','🎯 لول‌آپ سطح 20',20,'Level Up Package - Level 20',110000,9999),
 ('gem','🎯 لول‌آپ سطح 25',25,'Level Up Package - Level 25',110000,9999),
 ('gem','🎯 لول‌آپ سطح 30',30,'Level Up Package - Level 30',172000,9999),
 ('gem','💎 110 جم',110,'110',191000,9999),
 ('gem','💎 231 جم',231,'231',382000,9999),
 ('gem','📅 بسته هفتگی',90001,'Weekly Membership',430000,9999),
 ('gem','🏆 بویاه پس',90002,'Booyah Pass',640000,9999),
 ('gem','💎 583 جم',583,'583',956000,9999),
 ('gem','💎 1188 جم',1188,'1188',1913000,9999),
 ('gem','📆 بسته ماهانه',90003,'Monthly Membership',2106000,9999),
 ('gem','💎 2420 جم',2420,'2420',3824000,9999)
) AS seed(kind,title,amount,supplier_sku,price,stock)
WHERE NOT EXISTS (SELECT 1 FROM products);

DO $catalogue$
DECLARE
  item RECORD;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings WHERE key='g2bulk_catalogue_14_20260727'
  ) THEN
    FOR item IN SELECT * FROM (VALUES
      ('Level Up Package - Level 6',6,65000,0.296::numeric),
      ('Level Up Package - Level 10',10,110000,0.510::numeric),
      ('Level Up Package - Level 15',15,110000,0.510::numeric),
      ('Level Up Package - Level 20',20,110000,0.510::numeric),
      ('Level Up Package - Level 25',25,110000,0.510::numeric),
      ('Level Up Package - Level 30',30,172000,0.826::numeric),
      ('110',110,191000,0.935::numeric),
      ('231',231,382000,1.870::numeric),
      ('Weekly Membership',90001,430000,2.081::numeric),
      ('Booyah Pass',90002,640000,3.121::numeric),
      ('583',583,956000,4.675::numeric),
      ('1188',1188,1913000,9.350::numeric),
      ('Monthly Membership',90003,2106000,10.394::numeric),
      ('2420',2420,3824000,18.700::numeric)
    ) AS approved(sku,product_amount,sale_price,cost_usd)
    LOOP
      UPDATE products SET title=item.sku,amount=item.product_amount,
        price=item.sale_price,supplier_cost_usd=item.cost_usd,
        stock=9999,active=true
      WHERE kind='gem' AND supplier_sku=item.sku;
      IF NOT FOUND THEN
        INSERT INTO products(
          kind,title,amount,supplier_sku,supplier_cost_usd,price,stock,active
        ) VALUES (
          'gem',item.sku,item.product_amount,item.sku,item.cost_usd,
          item.sale_price,9999,true
        );
      END IF;
    END LOOP;

    INSERT INTO settings(key,value)
    VALUES('g2bulk_catalogue_14_20260727','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $catalogue$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings WHERE key='telegram_catalog_20260726'
  ) THEN
    UPDATE products SET title='💎 110 جم',supplier_sku='110',
      price=191000,stock=9999,active=true WHERE kind='gem' AND amount=110;
    UPDATE products SET title='💎 231 جم',supplier_sku='231',
      price=382000,stock=9999,active=true WHERE kind='gem' AND amount=231;
    UPDATE products SET title='💎 583 جم',supplier_sku='583',
      price=956000,stock=9999,active=true WHERE kind='gem' AND amount=583;
    UPDATE products SET title='💎 1188 جم',supplier_sku='1188',
      price=1913000,stock=9999,active=true WHERE kind='gem' AND amount=1188;
    UPDATE products SET title='💎 2420 جم',supplier_sku='2420',
      price=3824000,stock=9999,active=true WHERE kind='gem' AND amount=2420;

    INSERT INTO products(kind,title,description,price,stock,active)
    SELECT 'sense_pc','پک سنس PC','پک سنس مخصوص سیستم PC',1000000,9999,true
    WHERE NOT EXISTS (
      SELECT 1 FROM products WHERE kind='sense_pc' AND title='پک سنس PC'
    );
    INSERT INTO products(kind,title,description,price,stock,active)
    SELECT 'sense_pc','پک سنس PC + خدمات',
      'پک سنس PC همراه با خدمات',2200000,9999,true
    WHERE NOT EXISTS (
      SELECT 1 FROM products
      WHERE kind='sense_pc' AND title='پک سنس PC + خدمات'
    );

    INSERT INTO settings(key,value) VALUES('telegram_catalog_20260726','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $$;

DO $titles$
DECLARE
  item RECORD;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings
    WHERE key='g2bulk_catalogue_titles_fa_v2_20260727'
  ) THEN
    FOR item IN SELECT * FROM (VALUES
      ('Level Up Package - Level 6','🎯 لول‌آپ سطح 6'),
      ('Level Up Package - Level 10','🎯 لول‌آپ سطح 10'),
      ('Level Up Package - Level 15','🎯 لول‌آپ سطح 15'),
      ('Level Up Package - Level 20','🎯 لول‌آپ سطح 20'),
      ('Level Up Package - Level 25','🎯 لول‌آپ سطح 25'),
      ('Level Up Package - Level 30','🎯 لول‌آپ سطح 30'),
      ('110','💎 110 جم'),
      ('231','💎 231 جم'),
      ('Weekly Membership','📅 بسته هفتگی'),
      ('Booyah Pass','🏆 بویاه پس'),
      ('583','💎 583 جم'),
      ('1188','💎 1188 جم'),
      ('Monthly Membership','📆 بسته ماهانه'),
      ('2420','💎 2420 جم')
    ) AS approved(sku,display_title)
    LOOP
      UPDATE products SET title=item.display_title
      WHERE kind='gem' AND supplier_sku=item.sku;
    END LOOP;

    INSERT INTO settings(key,value)
    VALUES('g2bulk_catalogue_titles_fa_v2_20260727','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $titles$;

DO $package_titles$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings
    WHERE key='g2bulk_package_titles_fa_v3_20260728'
  ) THEN
    UPDATE products SET title='📅 بسته هفتگی'
    WHERE kind='gem' AND supplier_sku='Weekly Membership';
    UPDATE products SET title='📆 بسته ماهانه'
    WHERE kind='gem' AND supplier_sku='Monthly Membership';
    INSERT INTO settings(key,value)
    VALUES('g2bulk_package_titles_fa_v3_20260728','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $package_titles$;

-- ─── جم با اطلاعات (parity با تلگرام) ───────────────────────────────────────
ALTER TABLE products DROP CONSTRAINT IF EXISTS products_kind_check;
ALTER TABLE products
  ADD CONSTRAINT products_kind_check
  CHECK (kind IN ('gem','sense_mobile','sense_pc','store','gem_credentials'));

CREATE TABLE IF NOT EXISTS credential_orders (
  order_id BIGINT PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
  login_method TEXT NOT NULL DEFAULT '',
  ciphertext TEXT NOT NULL DEFAULT '',
  two_factor BOOLEAN NOT NULL DEFAULT false,
  cred_status TEXT NOT NULL DEFAULT 'awaiting_payment',
  viewed_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ,
  admin_note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_credential_orders_status
  ON credential_orders(cred_status);

ALTER TABLE tickets
  ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'bot';
ALTER TABLE tickets
  ADD COLUMN IF NOT EXISTS related_order_id BIGINT REFERENCES orders(id) ON DELETE SET NULL;

INSERT INTO settings(key,value) VALUES
  ('credential_support_id','@lookurback'),
  ('credential_profit_percent','40'),
  ('credential_weekly_profit_percent','40'),
  ('credential_monthly_profit_percent','40'),
  ('credential_weekly_cost_usd','1.328'),
  ('credential_monthly_cost_usd','6.64')
ON CONFLICT(key) DO NOTHING;

-- اگر خالی یا شناسه داخلی روبیکا بود، آیدی عمومی درست را بگذار (ادمین بعداً از پنل عوض می‌کند)
UPDATE settings SET value='@omid_1797', updated_at=now()
 WHERE key='support_id'
   AND (COALESCE(TRIM(value),'')='' OR value ~* '^u0');
UPDATE settings SET value='@lookurback', updated_at=now()
 WHERE key='credential_support_id'
   AND (COALESCE(TRIM(value),'')='' OR value ~* '^u0');

DO $cred_products$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM settings WHERE key='credential_products_seed_v1'
  ) THEN
    INSERT INTO products(kind,title,description,amount,price,stock,active,supplier_sku)
    SELECT 'gem_credentials','📅 عضویت هفتگی (جم با اطلاعات)',
      'تحویل دستی با اطلاعات ورود اکانت — هفتگی',60,500000,9999,true,'cred_weekly'
    WHERE NOT EXISTS (
      SELECT 1 FROM products WHERE kind='gem_credentials' AND supplier_sku='cred_weekly'
    );
    INSERT INTO products(kind,title,description,amount,price,stock,active,supplier_sku)
    SELECT 'gem_credentials','📆 عضویت ماهانه (جم با اطلاعات)',
      'تحویل دستی با اطلاعات ورود اکانت — ماهانه',300,2000000,9999,true,'cred_monthly'
    WHERE NOT EXISTS (
      SELECT 1 FROM products WHERE kind='gem_credentials' AND supplier_sku='cred_monthly'
    );
    INSERT INTO settings(key,value) VALUES('credential_products_seed_v1','1')
    ON CONFLICT(key) DO UPDATE SET value='1',updated_at=now();
  END IF;
END $cred_products$;

-- Older versions retried provider calls automatically. Their outcome may be
-- ambiguous, so quarantine them for reconciliation instead of resubmitting.
UPDATE fulfillments
SET status='SUBMIT_UNKNOWN',next_retry_at=NULL,updated_at=now()
WHERE status='RETRY' AND provider_order_id IS NULL;

-- پیش‌فرض قدیمی سود جم با آیدی ۷٪ بود؛ یک‌بار به ۱۰٪ ارتقا بده (اگر دستی عوض نشده).
UPDATE settings SET value='10', updated_at=now()
WHERE key='gem_profit_percent' AND value='7';
