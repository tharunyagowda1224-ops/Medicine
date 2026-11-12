import calendar
import math
import os
import re
import uuid
from datetime import date, datetime
from functools import wraps
from pathlib import Path

import pytesseract
pytesseract.pytesseract.tesseract_cmd=r'C:\Program Files\Tesseract-OCR\tesseract.exe'
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key",
)
app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + str(BASE_DIR / "medicine_inventory.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB maximum upload

db = SQLAlchemy(app)


# Database models

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    image_filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
    )


class PurchasePlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_name = db.Column(db.String(200), nullable=False)
    normalized_name = db.Column(db.String(200), nullable=False)
    average_monthly_use = db.Column(db.Integer, nullable=False)
    supplier_lead_days = db.Column(db.Integer, nullable=False, default=7)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "normalized_name",
            name="unique_user_purchase_plan",
        ),
    )


with app.app_context():
    db.create_all()


# Helpers

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def normalize_product_name(name):
    return " ".join(name.strip().lower().split())


def last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]


def date_from_text(text):
    """Parse common numeric and month-name date formats."""
    text = text.upper().replace("O", "0")

    # 1) YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    match = re.search(
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b",
        text,
    )
    if match:
        year, month, day = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # 2) DD/MM/YYYY / DD-MM-YYYY / DD.MM.YYYY
    match = re.search(
        r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b",
        text,
    )
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # 3) Numeric month/year with separators:
    #    06/2027, 06-2027, 06 2027, 06.2027
    match = re.search(
        r"\b(0?[1-9]|1[0-2])(?:[-/.]|\s+)(20\d{2}|\d{2})\b",
        text,
    )
    if match:
        month = int(match.group(1))
        year_text = match.group(2)
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        return date(year, month, calendar.monthrange(year, month)[1])

    # 4) Compact numeric month/year:
    #    0627, 062024, 062024
    match = re.search(
        r"\b(0?[1-9]|1[0-2])(?:20\d{2}|\d{2})\b",
        text,
    )
    if match:
        compact = match.group(0)
        if len(compact) == 4:
            month = int(compact[:2])
            year_text = compact[2:]
        elif len(compact) == 6:
            month = int(compact[:2])
            year_text = compact[2:]
        else:
            month = int(compact[:2])
            year_text = compact[2:]
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        return date(year, month, calendar.monthrange(year, month)[1])

    # 5) Month name + year:
    #    JUN 2027, JUNE 2027, JUN2027, JUN-2027, JUN/2027
    month_numbers = {
        "JAN": 1, "JANUARY": 1,
        "FEB": 2, "FEBRUARY": 2,
        "MAR": 3, "MARCH": 3,
        "APR": 4, "APRIL": 4,
        "MAY": 5,
        "JUN": 6, "JUNE": 6,
        "JUL": 7, "JULY": 7,
        "AUG": 8, "AUGUST": 8,
        "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
        "OCT": 10, "OCTOBER": 10,
        "NOV": 11, "NOVEMBER": 11,
        "DEC": 12, "DECEMBER": 12,
    }

    month_names = "|".join(
        sorted(month_numbers, key=len, reverse=True)
    )
    match = re.search(
        rf"\b({month_names})(?:[-/.]|\s*)(20\d{{2}}|\d{{2}})\b",
        text,
    )
    if match:
        month = month_numbers[match.group(1).upper()]
        year_text = match.group(2)
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        return date(year, month, calendar.monthrange(year, month)[1])

    match = re.search(
        rf"\b({month_names})(20\d{{2}}|\d{{2}})\b",
        text,
    )
    if match:
        month = month_numbers[match.group(1).upper()]
        year_text = match.group(2)
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        return date(year, month, calendar.monthrange(year, month)[1])

    return None


def extract_expiry_date(text):
    """
    Return a date only when it is on the same line as an expiry label.
    When manufacturing labels appear nearby, reject the manufacturing date.
    """
    text = text.replace("O", "0").replace("o", "0")

    expiry_pattern = re.compile(
        r"\b(?:EXP(?:IRY|IRATION)?|USE\s*BY|BEST\s*BEFORE|BBE)\b",
        re.IGNORECASE,
    )
    manufacturing_pattern = re.compile(
        r"\b(?:MFG|MFD|MFR|MANUFACTURED|DOM|PKD|PROD(?:UCTION)?)"
        r"(?:\s*DATE)?\b",
        re.IGNORECASE,
    )
    date_pattern = re.compile(
        r"\b(?:"
        r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}"
        r"|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}"
        r"|(?:0?[1-9]|1[0-2])(?:[-/.]|\s+)(?:20\d{2}|\d{2})"
        r"|(?:0?[1-9]|1[0-2])(?:20\d{2}|\d{2})"
        r"|(?:JANUARY|JAN|FEBRUARY|FEB|MARCH|MAR|APRIL|APR|MAY|"
        r"JUNE|JUN|JULY|JUL|AUGUST|AUG|SEPTEMBER|SEPT|SEP|"
        r"OCTOBER|OCT|NOVEMBER|NOV|DECEMBER|DEC)"
        r"(?:[-/.]|\s*)(?:20\d{2}|\d{2})"
        r")\b",
        re.IGNORECASE,
    )

    best_date = None
    best_distance = float("inf")

    for line in text.splitlines():
        expiry_matches = list(expiry_pattern.finditer(line))
        if not expiry_matches:
            continue

        manufacturing_matches = list(manufacturing_pattern.finditer(line))

        for date_match in date_pattern.finditer(line):
            candidate = date_from_text(date_match.group(0))
            if candidate is None:
                continue

            start, end = date_match.span()

            def distance(marker):
                if marker.end() < start:
                    return start - marker.end()
                if marker.start() > end:
                    return marker.start() - end
                return 0

            expiry_distance = min(distance(marker) for marker in expiry_matches)

            manufacturing_distance = (
                min(distance(marker) for marker in manufacturing_matches)
                if manufacturing_matches
                else float("inf")
            )

            # Ignore dates too far away from the expiry label.
            if expiry_distance > 40:
                continue

            # Reject a date if an MFG/MFD label is equally or more nearby.
            if manufacturing_distance <= expiry_distance:
                continue

            if expiry_distance < best_distance:
                best_date = candidate
                best_distance = expiry_distance

    return best_date


def extract_product_name(text):
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" :-|")

        if len(line) < 3 or len(line) > 100:
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        if re.search(
            r"\b(EXP|EXPIRY|EXPIRES|MFG|MFD|MFR|MANUFACTURED|"
            r"BATCH|LOT|USE BY|BEST BEFORE)\b",
            line,
            re.IGNORECASE,
        ):
            continue

        return line

    return "Unknown product"


def assess_expiry(expiry_date):
    days_left = (expiry_date - date.today()).days

    if days_left < 0:
        return (
            days_left,
            "Expired",
            "Do not use expired medicine; follow local disposal rules.",
        )

    if days_left <= 7:
        return (
            days_left,
            "High",
            "Expiry is approaching. Review stock promptly.",
        )

    if days_left <= 30:
        return (
            days_left,
            "Medium",
            "Plan stock use or redistribution before expiry.",
        )

    return (
        days_left,
        "Low",
        "Continue routine stock checks.",
    )


# Authentication routes

@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3 or len(password) < 8:
            flash(
                "Use a username of at least 3 characters and a password "
                "of at least 8 characters."
            )
            return render_template("auth.html", title="Register")

        if User.query.filter_by(username=username).first():
            flash("That username is already registered.")
            return render_template("auth.html", title="Register")

        user = User(
            username=username,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        session.clear()
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    return render_template("auth.html", title="Register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username or password.")
            return render_template("auth.html", title="Login")

        session.clear()
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    return render_template("auth.html", title="Login")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# Product routes

@app.route("/products", methods=["POST"])
@login_required
def add_product():
    upload = request.files.get("image")

    if not upload or not upload.filename:
        flash("Choose a product image.")
        return redirect(url_for("dashboard"))

    try:
        image = Image.open(upload.stream)
        image.load()
        image = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        flash("Could not read the image. Try a clear JPG, PNG, or WEBP photo.")
        return redirect(url_for("dashboard"))

    try:
        image_for_ocr = image.copy()

        if image_for_ocr.width < 1600:
            scale = 1600 / image_for_ocr.width
            image_for_ocr = image_for_ocr.resize(
                (
                    1600,
                    int(image_for_ocr.height * scale),
                ),
                Image.Resampling.LANCZOS,
            )

        image_for_ocr = ImageOps.autocontrast(
            ImageOps.grayscale(image_for_ocr)
        )

        text_block = pytesseract.image_to_string(
            image_for_ocr,
            config="--oem 3 --psm 6",
        )
        text_sparse = pytesseract.image_to_string(
            image_for_ocr,
            config="--oem 3 --psm 11",
        )
        ocr_text = text_block + "\n" + text_sparse

        app.logger.info("OCR text detected: %r", ocr_text)

    except pytesseract.TesseractNotFoundError:
        flash(
            "Tesseract OCR was not found. Install Tesseract and make sure "
            "it is available on PATH."
        )
        return redirect(url_for("dashboard"))
    except pytesseract.TesseractError:
        app.logger.exception("Tesseract OCR failed")
        flash("OCR could not process this image. Try a clearer, closer photo.")
        return redirect(url_for("dashboard"))

    expiry_date = extract_expiry_date(ocr_text)

    if expiry_date is None:
        flash(
            "Expiry date could not be confidently identified next to an "
            "expiry label. Try a clearer photo showing the EXP/Expiry text "
            "and its date together."
        )
        return redirect(url_for("dashboard"))

    product_name = request.form.get("name", "").strip()
    if not product_name:
        product_name = extract_product_name(ocr_text)
    product_name = product_name[:200]

    try:
        quantity = int(request.form.get("quantity", "1"))
        if quantity < 1:
            raise ValueError
    except ValueError:
        flash("Quantity must be a positive whole number.")
        return redirect(url_for("dashboard"))

    filename = f"{uuid.uuid4().hex}.jpg"
    image_path = UPLOAD_DIR / filename

    try:
        image.save(image_path, format="JPEG", quality=90)
    except OSError:
        flash(
            "Could not save the image. Check that static/uploads exists "
            "and is writable."
        )
        return redirect(url_for("dashboard"))

    product = Product(
        user_id=session["user_id"],
        name=product_name,
        expiry_date=expiry_date,
        quantity=quantity,
        image_filename=filename,
    )

    try:
        db.session.add(product)
        db.session.commit()
    except Exception:
        db.session.rollback()
        image_path.unlink(missing_ok=True)
        app.logger.exception("Could not save product")
        flash("Could not save the product to the database.")
        return redirect(url_for("dashboard"))

    flash(
        f"Product saved. Detected expiry date {expiry_date.isoformat()}. "
        "Please verify it against the package."
    )
    return redirect(url_for("dashboard"))


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    product = Product.query.filter_by(
        id=product_id,
        user_id=session["user_id"],
    ).first_or_404()

    image_path = UPLOAD_DIR / product.image_filename

    db.session.delete(product)
    db.session.commit()
    image_path.unlink(missing_ok=True)

    flash("Product deleted.")
    return redirect(url_for("dashboard"))


@app.route("/purchase-plans", methods=["POST"])
@login_required
def save_purchase_plan():
    product_name = request.form.get("product_name", "").strip()

    if not product_name:
        flash("Enter the medicine name for the purchase plan.")
        return redirect(url_for("dashboard"))

    try:
        monthly_use = int(request.form.get("average_monthly_use", "0"))
        lead_days = int(request.form.get("supplier_lead_days", "7"))
    except ValueError:
        flash("Monthly use and supplier lead time must be whole numbers.")
        return redirect(url_for("dashboard"))

    if monthly_use < 1:
        flash("Average monthly use must be at least 1.")
        return redirect(url_for("dashboard"))

    if lead_days < 0 or lead_days > 365:
        flash("Supplier lead time must be between 0 and 365 days.")
        return redirect(url_for("dashboard"))

    normalized_name = normalize_product_name(product_name)

    plan = PurchasePlan.query.filter_by(
        user_id=session["user_id"],
        normalized_name=normalized_name,
    ).first()

    if plan:
        plan.product_name = product_name
        plan.average_monthly_use = monthly_use
        plan.supplier_lead_days = lead_days
    else:
        plan = PurchasePlan(
            user_id=session["user_id"],
            product_name=product_name,
            normalized_name=normalized_name,
            average_monthly_use=monthly_use,
            supplier_lead_days=lead_days,
        )
        db.session.add(plan)

    db.session.commit()
    flash(f"Purchase plan saved for {product_name}.")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]

    products = (
        Product.query
        .filter_by(user_id=user_id)
        .order_by(Product.expiry_date.asc())
        .all()
    )

    plans = PurchasePlan.query.filter_by(user_id=user_id).all()

    stock_by_name = {}
    rows = []
    alerts = []
    expired_total = 0
    near_expiry_total = 0

    for product in products:
        days_left, risk_status, recommendation = assess_expiry(
            product.expiry_date
        )

        if days_left < 0:
            expired_total += product.quantity
            alerts.append(
                f"{product.name} is expired. Follow disposal rules."
            )
        elif days_left <= 30:
            near_expiry_total += product.quantity
            alerts.append(
                f"{product.name} expires in {days_left} day(s)."
            )

        if days_left >= 0:
            key = normalize_product_name(product.name)
            stock_by_name[key] = stock_by_name.get(key, 0) + product.quantity

        rows.append({
            "product": product,
            "days_left": days_left,
            "risk_status": risk_status,
            "recommendation": recommendation,
        })

    purchase_recommendations = []

    for plan in plans:
        current_stock = stock_by_name.get(plan.normalized_name, 0)

        coverage_days = plan.supplier_lead_days + 30
        target_stock = math.ceil(
            plan.average_monthly_use * coverage_days / 30
        )
        recommended_quantity = max(0, target_stock - current_stock)

        purchase_recommendations.append({
            "plan": plan,
            "current_stock": current_stock,
            "target_stock": target_stock,
            "recommended_quantity": recommended_quantity,
        })

        if recommended_quantity > 0:
            alerts.append(
                f"Purchase estimate for {plan.product_name}: "
                f"{recommended_quantity} unit(s)."
            )

    return render_template(
        "dashboard.html",
        rows=rows,
        alerts=alerts,
        near_expiry_total=near_expiry_total,
        expired_total=expired_total,
        purchase_recommendations=purchase_recommendations,
    )


if __name__ == "__main__":
    app.run(debug=True)