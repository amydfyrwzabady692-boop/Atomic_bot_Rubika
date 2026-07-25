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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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
 ('support_id',''),
 ('zarinpal_enabled','1'),
 ('card_enabled','1'),
 ('card_number',''),
 ('card_holder',''),
 ('card_bank',''),
 ('usd_toman_rate','')
ON CONFLICT (key) DO NOTHING;
INSERT INTO departments(title) VALUES ('عمومی'),('مالی'),('پیگیری سفارش')
ON CONFLICT (title) DO NOTHING;
INSERT INTO categories(title) VALUES ('جم فری‌فایر'),('سنسیویتی موبایل'),('سنسیویتی PC')
ON CONFLICT DO NOTHING;
INSERT INTO products(kind,title,amount,supplier_sku,price,stock)
SELECT * FROM (VALUES
 ('gem','۱۱۰ جم',110,'110 Diamonds',75000,9999),
 ('gem','۲۳۱ جم',231,'231 Diamonds',150000,9999),
 ('gem','۵۸۳ جم',583,'583 Diamonds',365000,9999),
 ('gem','۱۱۸۸ جم',1188,'1188 Diamonds',730000,9999),
 ('gem','۲۴۲۰ جم',2420,'2420 Diamonds',1450000,9999)
) AS seed(kind,title,amount,supplier_sku,price,stock)
WHERE NOT EXISTS (SELECT 1 FROM products);
