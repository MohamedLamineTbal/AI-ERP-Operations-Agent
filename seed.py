"""Create the ERP tables and fill an empty database with sample data."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from random import Random

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import engine
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
    ("Ergonomic Office Chair", "FUR-CHAIR-001", "249.90", 7, 10),
    ("Standing Desk 140 cm", "FUR-DESK-002", "489.00", 18, 8),
    ("LED Desk Lamp", "OFF-LAMP-003", "39.95", 4, 12),
    ("Wireless Keyboard", "IT-KEY-004", "64.50", 45, 15),
    ("Wireless Mouse", "IT-MOUSE-005", "34.90", 9, 15),
    ("27-inch Monitor", "IT-MON-006", "319.00", 21, 7),
    ("USB-C Docking Station", "IT-DOCK-007", "139.75", 5, 10),
    ("Business Laptop 14-inch", "IT-LAP-008", "1149.00", 14, 5),
    ("Noise-Cancelling Headset", "IT-AUD-009", "179.90", 28, 10),
    ("A4 Copy Paper Case", "OFF-PAPER-010", "44.25", 75, 20),
    ("Black Toner Cartridge", "OFF-TONER-011", "89.00", 6, 9),
    ("Color Toner Set", "OFF-TONER-012", "229.00", 11, 6),
    ("Heavy-Duty Stapler", "OFF-STAPLE-013", "28.50", 35, 10),
    ("Whiteboard 120 cm", "OFF-BOARD-014", "119.00", 8, 5),
    ("Document Shredder", "OFF-SHRED-015", "159.50", 3, 6),
    ("Wi-Fi 6 Router", "NET-ROUTER-016", "129.90", 19, 8),
    ("8-Port Network Switch", "NET-SWITCH-017", "74.00", 24, 8),
    ("Cat6 Cable 10 m", "NET-CABLE-018", "18.75", 90, 25),
    ("External SSD 1 TB", "IT-SSD-019", "124.90", 13, 10),
    ("Webcam 1080p", "IT-CAM-020", "79.50", 5, 9),
    ("Conference Speakerphone", "IT-CONF-021", "209.00", 12, 5),
    ("Portable Projector", "IT-PROJ-022", "599.00", 2, 4),
    ("Label Printer", "OFF-LABEL-023", "149.00", 17, 6),
    ("Shipping Label Roll", "OFF-ROLL-024", "24.50", 60, 20),
    ("Barcode Scanner", "OPS-SCAN-025", "99.90", 7, 10),
    ("Warehouse Tablet", "OPS-TAB-026", "379.00", 16, 5),
    ("Safety Vest Pack", "OPS-VEST-027", "42.00", 38, 15),
    ("First Aid Kit", "OPS-AID-028", "56.50", 8, 8),
    ("Thermal Receipt Printer", "POS-PRINT-029", "189.00", 4, 7),
    ("Cash Drawer", "POS-CASH-030", "109.00", 22, 6),
]


def seed_database() -> None:
    """Create the schema and seed it once when the ERP tables are empty."""
    Base.metadata.create_all(engine)

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
                stock_quantity=stock,
                reorder_threshold=threshold,
            )
            for name, sku, price, stock, threshold in PRODUCTS
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
