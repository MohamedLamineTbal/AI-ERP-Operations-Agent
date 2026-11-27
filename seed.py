"""Create the ERP tables and the app's two logins, and fill an empty database with sample data."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from random import Random

from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import DATABASE_ADMIN_URL, DATABASE_URL, READONLY_DATABASE_URL
from models import (
    Base,
    Customer,
    Invoice,
    Order,
    OrderItem,
    Product,
    PurchaseOrder,
)


CUSTOMERS = [
    ("Amina El Idrissi", "Atlas Retail", "amina@atlasretail.example"),
    ("Youssef Benali", "Casablanca Office Co.", "youssef@casaoffice.example"),
    ("Sara Alaoui", "Rif Hospitality", "sara@rifhospitality.example"),
    ("Omar Tazi", "Marrakech Market", "omar@marrakechmarket.example"),
    ("Nadia Berrada", "Blue Gate Logistics", "nadia@bluegate.example"),
    ("Karim Amrani", "Sahara Supplies", "karim@saharasupplies.example"),
    ("Leila Chraibi", "Medina Design Studio", "leila@medinadesign.example"),
    ("Mehdi Lahlou", "North Coast Foods", "mehdi@northcoast.example"),
    ("Salma Fassi", "Argan Wellness", "salma@arganwellness.example"),
    ("Hamza Bennani", "Green Valley Farms", "hamza@greenvalley.example"),
    ("Imane Kettani", "Maghreb Consulting", "imane@maghrebconsulting.example"),
    ("Rachid Sqalli", "Red City Construction", "rachid@redcity.example"),
    ("Meryem Filali", "Ocean View Hotels", "meryem@oceanview.example"),
    ("Adil Kadiri", "Precision Auto Parts", "adil@precisionauto.example"),
    ("Hind Naciri", "Cedar Education", "hind@cedareducation.example"),
    ("Zakaria Bennis", "Gateway Electronics", "zakaria@gateway.example"),
    ("Ghita Lamrani", "Palm Event Group", "ghita@palmevents.example"),
    ("Anas El Mansouri", "Terra Home Goods", "anas@terrahome.example"),
    ("Sofia Ouazzani", "Andalus Fashion", "sofia@andalusfashion.example"),
    ("Bilal Idrissi", "Summit Sports", "bilal@summitsports.example"),
    ("Kawtar Zniber", "Urban Bean Cafes", "kawtar@urbanbean.example"),
    ("Ayoub Gharbi", "Nova Security", "ayoub@novasecurity.example"),
    ("Lina Cherkaoui", "Bright Dental", "lina@brightdental.example"),
    ("Samir Belkadi", "Portside Trading", "samir@portside.example"),
]

PRODUCTS = [
    ("Ergonomic Office Chair", "FUR-CHAIR-001", "249.90", "144.94", 7, 10),
    ("Standing Desk 140 cm", "FUR-DESK-002", "489.00", "283.62", 18, 8),
    ("LED Desk Lamp", "OFF-LAMP-003", "39.95", "21.97", 4, 12),
    ("Wireless Keyboard", "IT-KEY-004", "64.50", "46.44", 45, 15),
    ("Wireless Mouse", "IT-MOUSE-005", "34.90", "25.13", 9, 15),
    ("27-inch Monitor", "IT-MON-006", "319.00", "229.68", 21, 7),
    ("USB-C Docking Station", "IT-DOCK-007", "139.75", "100.62", 5, 10),
    ("Business Laptop 14-inch", "IT-LAP-008", "1149.00", "827.28", 14, 5),
    ("Noise-Cancelling Headset", "IT-AUD-009", "179.90", "129.53", 28, 10),
    ("A4 Copy Paper Case", "OFF-PAPER-010", "44.25", "24.34", 75, 20),
    ("Black Toner Cartridge", "OFF-TONER-011", "89.00", "48.95", 6, 9),
    ("Color Toner Set", "OFF-TONER-012", "229.00", "125.95", 11, 6),
    ("Heavy-Duty Stapler", "OFF-STAPLE-013", "28.50", "15.68", 35, 10),
    ("Whiteboard 120 cm", "OFF-BOARD-014", "119.00", "65.45", 8, 5),
    ("Document Shredder", "OFF-SHRED-015", "159.50", "87.73", 3, 6),
    ("Wi-Fi 6 Router", "NET-ROUTER-016", "129.90", "80.54", 19, 8),
    ("8-Port Network Switch", "NET-SWITCH-017", "74.00", "45.88", 24, 8),
    ("Cat6 Cable 10 m", "NET-CABLE-018", "18.75", "11.63", 90, 25),
    ("External SSD 1 TB", "IT-SSD-019", "124.90", "89.93", 13, 10),
    ("Webcam 1080p", "IT-CAM-020", "79.50", "57.24", 5, 9),
    ("Conference Speakerphone", "IT-CONF-021", "209.00", "150.48", 12, 5),
    ("Portable Projector", "IT-PROJ-022", "599.00", "431.28", 2, 4),
    ("Label Printer", "OFF-LABEL-023", "149.00", "81.95", 17, 6),
    ("Shipping Label Roll", "OFF-ROLL-024", "24.50", "13.48", 60, 20),
    ("Barcode Scanner", "OPS-SCAN-025", "99.90", "63.94", 7, 10),
    ("Warehouse Tablet", "OPS-TAB-026", "379.00", "242.56", 16, 5),
    ("Safety Vest Pack", "OPS-VEST-027", "42.00", "26.88", 38, 15),
    ("First Aid Kit", "OPS-AID-028", "56.50", "36.16", 8, 8),
    ("Thermal Receipt Printer", "POS-PRINT-029", "189.00", "115.29", 4, 7),
    ("Cash Drawer", "POS-CASH-030", "109.00", "66.49", 22, 6),
]


def create_admin_engine() -> Engine:
    """Return an engine for the login that owns the schema and creates roles."""
    if not DATABASE_ADMIN_URL:
        raise RuntimeError(
            "DATABASE_ADMIN_URL is not set. Add the URL of a login that can "
            "create tables and roles to .env; only seed.py uses it."
        )
    return create_engine(DATABASE_ADMIN_URL)


def _run_quoted(connection: Connection, statements: list[tuple[str, list[str]]]) -> None:
    """Run each (format template, arguments) pair, quoted by PostgreSQL's format()."""
    for template, arguments in statements:
        names = [f"a{i}" for i in range(len(arguments))]
        casts = ", ".join(f"CAST(:{name} AS text)" for name in names)
        try:
            quoted = connection.scalar(
                text(f"SELECT format(CAST(:template AS text), {casts})"),
                {"template": template, **dict(zip(names, arguments))},
            )
            connection.exec_driver_sql(quoted)
        except SQLAlchemyError as error:
            # Re-raise without the statement, which may hold a password.
            raise RuntimeError(
                f"Could not set up the database logins: {type(error.orig).__name__}"
            ) from None


def _reset_login(connection: Connection, url: str) -> list[tuple[str, list[str]]]:
    """Statements that create or update a login with no rights of its own."""
    login = make_url(url)
    role = login.username
    exists = connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    verb = "ALTER" if exists else "CREATE"
    return [
        (
            f"{verb} ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L",
            [role, login.password],
        ),
        ("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I", [role]),
        ("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I", [role]),
        ("GRANT CONNECT ON DATABASE %I TO %I", [login.database, role]),
        ("GRANT USAGE ON SCHEMA public TO %I", [role]),
    ]


def grant_logins(connection: Connection) -> None:
    """Create or update the two logins the running app uses.

    The read-only login may only SELECT the ERP tables. The app login may also
    make exactly the writes the business actions make, and nothing else: no
    deletes, no schema changes, no roles.
    """
    usernames = [
        make_url(url).username
        for url in (DATABASE_ADMIN_URL, DATABASE_URL, READONLY_DATABASE_URL)
    ]
    if len(set(usernames)) != len(usernames):
        raise RuntimeError(
            "DATABASE_ADMIN_URL, DATABASE_URL and READONLY_DATABASE_URL must "
            "each use a different login."
        )
    _, app, readonly = usernames
    tables = [table.name for table in Base.metadata.sorted_tables]
    sequence = connection.scalar(
        text("SELECT pg_get_serial_sequence('purchase_orders', 'id')")
    )

    _run_quoted(
        connection,
        [
            *_reset_login(connection, READONLY_DATABASE_URL),
            *[("GRANT SELECT ON %I TO %I", [table, readonly]) for table in tables],
            *_reset_login(connection, DATABASE_URL),
            *[("GRANT SELECT ON %I TO %I", [table, app]) for table in tables],
            ("GRANT INSERT ON purchase_orders TO %I", [app]),
            ("GRANT USAGE ON SEQUENCE %s TO %I", [sequence, app]),
            ("GRANT UPDATE (status, cancellation_reason) ON orders TO %I", [app]),
            ("GRANT UPDATE (reminder_sent_at) ON invoices TO %I", [app]),
        ],
    )


def seed_database() -> None:
    """Create the schema and logins, and seed once when the ERP tables are empty."""
    engine = create_admin_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        grant_logins(connection)

    with Session(engine) as session:
        existing_customers = session.scalar(select(func.count(Customer.id))) or 0
        if existing_customers:
            print("Seed skipped: ERP sample data already exists.")
            return

        now = datetime.now(timezone.utc)
        today = date.today()
        rng = Random(20260825)

        customers = [
            Customer(
                name=name,
                company=company,
                email=email,
                created_at=now - timedelta(days=365 - index * 9),
            )
            for index, (name, company, email) in enumerate(CUSTOMERS)
        ]
        products = [
            Product(
                name=name,
                sku=sku,
                price=Decimal(price),
                unit_cost=Decimal(unit_cost),
                stock_quantity=stock,
                reorder_threshold=threshold,
            )
            for name, sku, price, unit_cost, stock, threshold in PRODUCTS
        ]
        session.add_all(customers + products)
        session.flush()

        order_statuses = [
            "completed",
            "shipped",
            "processing",
            "pending",
            "completed",
            "completed",
            "cancelled",
        ]
        orders = []
        for index in range(60):
            order = Order(
                customer=customers[(index * 7) % len(customers)],
                status=order_statuses[index % len(order_statuses)],
                total_amount=Decimal("0.00"),
                created_at=now - timedelta(days=2 + index * 3),
            )
            selected_products = rng.sample(products, 2 + index % 4)
            total = Decimal("0.00")
            for product in selected_products:
                quantity = rng.randint(1, 5)
                order.items.append(
                    OrderItem(
                        product=product,
                        quantity=quantity,
                        unit_price=product.price,
                    )
                )
                total += product.price * quantity
            order.total_amount = total.quantize(Decimal("0.01"))
            orders.append(order)

        session.add_all(orders)
        session.flush()

        invoices = []
        invoice_orders = [order for order in orders if order.status != "cancelled"][:40]
        for index, order in enumerate(invoice_orders):
            if index < 8:
                status = "overdue"
                due_date = today - timedelta(days=7 + index * 4)
            elif index < 20:
                status = "paid"
                due_date = today - timedelta(days=index - 5)
            elif index < 32:
                status = "sent"
                due_date = today + timedelta(days=7 + index)
            else:
                status = "draft"
                due_date = today + timedelta(days=30 + index)

            invoices.append(
                Invoice(
                    customer=order.customer,
                    order=order,
                    amount=order.total_amount,
                    status=status,
                    due_date=due_date,
                )
            )

        low_stock_products = [
            product
            for product in products
            if product.stock_quantity <= product.reorder_threshold
        ]
        purchase_orders = [
            PurchaseOrder(
                product=product,
                quantity=max(20, product.reorder_threshold * 3),
                unit_cost=product.unit_cost,
                status="ordered" if index % 3 else "draft",
                reason="Low-stock replenishment",
                created_at=now - timedelta(days=index),
            )
            for index, product in enumerate(low_stock_products)
        ]
        purchase_orders.extend(
            PurchaseOrder(
                product=products[index],
                quantity=25 + index * 5,
                unit_cost=products[index].unit_cost,
                status="received",
                reason="Regular inventory replenishment",
                created_at=now - timedelta(days=40 + index),
            )
            for index in range(5)
        )

        session.add_all(invoices + purchase_orders)
        session.commit()

        print(
            "Seeded "
            f"{len(customers)} customers, {len(products)} products, "
            f"{len(orders)} orders, "
            f"{sum(len(order.items) for order in orders)} order items, "
            f"{len(invoices)} invoices, and "
            f"{len(purchase_orders)} purchase orders."
        )


if __name__ == "__main__":
    seed_database()
