from flask import Flask, render_template, request, send_file, jsonify
import pandas as pd
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
import tempfile
import os
import glob
import uuid
import shutil
import traceback

import io
import csv
import json
from datetime import datetime, timedelta
from calendar import month_name, month_abbr
import re

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

REQUIRED_COLUMNS = [
    'Product Name', 'FSN', 'SKU Id', 'Brand', 'Quantity Sent', 'MRP',
    'Net Quantity', 'Generic Name', 'Month & Year of Manufacturing',
    'Manufactured by / Marketed by', 'Customer Care Details',
    'EAN/FSN/LID Barcode', 'Dimensions (cm)', 'Size'
]

UNNECESSARY_COLUMNS = [
    'Style Code', 'Color', 'Isbn', 'Model Id',
    'Quantity Received', 'Inwarded to Store', 'QC Fail', 'QC In Progress',
    'QC Passed', 'Cost Price', 'Length(In cms)', 'Breadth(In cms)',
    'Height(In cms)', 'Weight(In kgs)',
]

STATIC_DEFAULTS = {
    'Net Quantity': '1 unit',
    'Customer Care Details': 'email us at- xidlzzzzzz@gmail.com',
    'EAN/FSN/LID Barcode': '',
    'Size': 'medium',
}

# --- Editable column mappings for returns processing ---
# Keys = internal names used in output; Values = source column names in input files
MEESHO_COLUMNS_CONFIG = {
    'SKU': 'SKU',
    'Suborder Number': 'Suborder Number',
    'Type of Return': 'Type of Return',
    'Delivered Date': 'Delivered Date',
    'AWB Number': 'AWB Number',
    'Return Reason': 'Return Reason',
}

MEESHO_HEADER_MARKER = 'sku'

MEESHO_FILTERS = {
    'Type of Return': 'Customer Return',
    'Return Reason': 'Received wrong product (different color / size / product)',
}

# Flipkart config
FLIPKART_COLUMNS_CONFIG = {
    'SKU': 'SKU',
    'Suborder Number': 'Order ID',
    'Type of Return': 'Return Type',
    'Delivered Date': 'Out For Delivery Date',
    'AWB Number': 'Tracking ID',
    'Return Reason': 'Return Reason',
}
FLIPKART_HEADER_MARKER = 'sku'
FLIPKART_FILTERS = {
    'Return Type': 'customer_return',
    'Return Reason': 'MISSHIPMENT',
}

MONTH_ABBR_MAP = {name.lower(): i for i, name in enumerate(month_name) if i}
MONTH_ABBR_MAP.update({name.lower(): i for i, name in enumerate(month_abbr) if i})

def normalize_mfg_date(raw):
    raw = str(raw).strip()
    m = re.search(r'([A-Za-z]+)[\s\-/]*([0-9]{2,4})', raw)
    if not m:
        return raw
    month_str, year_str = m.group(1), m.group(2)
    month_num = MONTH_ABBR_MAP.get(month_str.lower())
    if not month_num:
        return raw
    full_month = month_name[month_num]
    year = int(year_str)
    if year < 100:
        year += 2000
    return f"{full_month} {year}"

def get_last_month_str():
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    return f"{month_name[last_month.month]} {last_month.year}"

LABEL_FIELD_MAP = {
    'model_number': 'SKU Id',
    'brand': 'Brand',
    'Net Quantity - 1 U': 'Net Quantity',
    'Size - If Applicable': 'Size',
    'Dimensions (in mm/cm) - If Applicable': 'Dimensions (cm)',
    'Generic Name': 'Generic Name',
    'Month & Year of Manufacturing': 'Month & Year of Manufacturing',
    'Manufactured by / Marketed by': 'Manufactured by / Marketed by',
    'Customer Care Details': 'Customer Care Details',
    'EAN/FSN/LID Barcode': 'EAN/FSN/LID Barcode',
    'title': 'Product Name',
    'poster_code': 'Poster Code',
    'mrp': 'MRP',
}

LABEL_FIELD_MAP_LOWER = {k.lower(): v for k, v in LABEL_FIELD_MAP.items()}

MRP_FIELD_PREFIX = 'mrp rs'

def parse_labels_csv(filepath):
    categories = {}
    current_cat = None
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                current_cat = None
                continue
            if not line.startswith('\t') and not line.startswith(' '):
                current_cat = stripped.lower()
                categories[current_cat] = []
            elif current_cat is not None:
                field = stripped
                if field:
                    categories[current_cat].append(field)
    return categories

def read_qc_files(qc_files, tmp_dir):
    sku_data = {}
    generic_names = {}
    for qc_file in qc_files:
        fname = qc_file.filename or ''
        base_name = os.path.basename(fname)
        base_lower = base_name.lower()
        if base_lower == 'labels.csv':
            continue
        if not base_lower.startswith('quality_check') or not base_lower.endswith('.csv'):
            continue
        qc_path = os.path.join(tmp_dir, 'qc_' + base_name)
        qc_file.save(qc_path)
        try:
            qc_df = pd.read_csv(qc_path)
            qc_df.columns = qc_df.columns.str.strip()
            generic_name = base_lower.replace('quality_check', '').replace('.csv', '').strip().lstrip('_').strip()
            sku_col = None
            for col in qc_df.columns:
                cl = col.strip().lower()
                if cl == 'sku' or cl == 'sku id' or cl == 'sku_id' or cl == 'skuid':
                    sku_col = col
                    break
            if not sku_col:
                print(f"[QC] WARNING: No SKU column found in {base_name}. Columns: {list(qc_df.columns)}")
                continue
            print(f"[QC] {base_name}: generic={generic_name}, sku_col={sku_col}, rows={len(qc_df)}")
            for _, row in qc_df.iterrows():
                sku_val = str(row[sku_col]).strip().lower()
                if not sku_val or sku_val == 'nan':
                    continue
                row_data = {}
                for col in qc_df.columns:
                    row_data[col.strip().lower()] = row[col] if pd.notna(row[col]) else ''
                sku_data[sku_val] = row_data
                generic_names[sku_val] = generic_name
        except Exception as e:
            print(f"[QC] Error reading {base_name}: {e}")
    return sku_data, generic_names

def read_qc_files_from_disk(saved_files, tmp_dir):
    sku_data = {}
    generic_names = {}
    for dest, base_lower in saved_files:
        if base_lower == 'labels.csv':
            continue
        if not base_lower.endswith('.csv'):
            continue
        try:
            try:
                qc_df = pd.read_csv(dest, sep=None, engine='python')
            except Exception:
                qc_df = pd.read_csv(dest)
            qc_df.columns = qc_df.columns.str.strip()
            generic_name = base_lower.replace('.csv', '').strip()
            generic_name = generic_name.replace('quality_check', '').lstrip('_').strip()
            generic_name = generic_name.replace('qc_', '').strip()
            sku_col = None
            for col in qc_df.columns:
                cl = col.strip().lower()
                if cl == 'sku' or cl == 'sku id' or cl == 'sku_id' or cl == 'skuid':
                    sku_col = col
                    break
            if not sku_col:
                print(f"[QC] WARNING: No SKU column found in {base_lower}. Columns: {list(qc_df.columns)}")
                continue
            print(f"[QC] {base_lower}: generic={generic_name}, sku_col={sku_col}, rows={len(qc_df)}")
            for _, row in qc_df.iterrows():
                sku_val = str(row[sku_col]).strip().lower()
                if not sku_val or sku_val == 'nan':
                    continue
                row_data = {}
                for col in qc_df.columns:
                    row_data[col.strip().lower()] = row[col] if pd.notna(row[col]) else ''
                sku_data[sku_val] = row_data
                generic_names[sku_val] = generic_name
        except Exception as e:
            print(f"[QC] Error reading {base_lower}: {e}")
    return sku_data, generic_names

def compute_dynamic_defaults(row, col):
    sku_id = str(row.get('SKU Id', '')).strip().lower() if pd.notna(row.get('SKU Id', '')) else ''
    brand = str(row.get('Brand', '')).strip().lower() if pd.notna(row.get('Brand', '')) else ''

    if col == 'Manufactured by / Marketed by':
        if 'jb creations' in brand:
            return 'JB,MadhyaPradesh-474002'
        elif 'xidlz' in brand:
            return 'XIDLZ,MadhyaPradesh-474002'
        return 'JB,MadhyaPradesh-474002'

    if col == 'Dimensions (cm)':
        if 'ch' in sku_id:
            return '35*25*2 cm'
        elif 'baby' in sku_id:
            return '35*6*6 cm'
        return '48*16*3 cm'

    if col == 'Generic Name':
        if 'baby' in sku_id:
            return 'poster'
        return 'painting'

    if col == 'Month & Year of Manufacturing':
        return get_last_month_str()

    return STATIC_DEFAULTS.get(col, '')

def fill_qc_and_defaults(df, labels_file, qc_files, tmp_dir):
    all_qc_data = {}
    label_categories = {}
    print(f"[QC] fill_qc_and_defaults called. labels_file: {labels_file.filename if labels_file else None}, qc_files count: {len(qc_files) if qc_files else 0}")

    if labels_file and labels_file.filename:
        labels_path = os.path.join(tmp_dir, 'labels.csv')
        try:
            labels_file.save(labels_path)
            label_categories = parse_labels_csv(labels_path)
            print(f"[QC] labels.csv parsed: {list(label_categories.keys())}")
        except Exception as e:
            print(f"[QC] labels.csv error: {e}")

    if qc_files:
        for qc_file in qc_files:
            fname = qc_file.filename or ''
            print(f"[QC] Found QC file: {fname}")
            dest = os.path.join(tmp_dir, 'qc_' + os.path.basename(fname).lower().replace('/', '_').replace('\\', '_'))
            try:
                qc_file.save(dest)
                try:
                    qc_df = pd.read_csv(dest, sep=None, engine='python')
                except Exception:
                    qc_df = pd.read_csv(dest)
                qc_df.columns = qc_df.columns.str.strip()
                print(f"[QC] {fname}: columns={list(qc_df.columns)}, rows={len(qc_df)}")
                sku_col = None
                for col in qc_df.columns:
                    cl = col.strip().lower()
                    if cl in ('sku', 'sku id', 'sku_id', 'skuid', 'model_number', 'model number'):
                        sku_col = col
                        break
                if not sku_col:
                    print(f"[QC] WARNING: No SKU column found in {fname}")
                    continue
                for _, r in qc_df.iterrows():
                    sku_val = str(r[sku_col]).strip().lower()
                    if not sku_val or sku_val == 'nan':
                        continue
                    if sku_val not in all_qc_data:
                        all_qc_data[sku_val] = {}
                    for col in qc_df.columns:
                        val = r[col] if pd.notna(r[col]) else ''
                        key = col.strip().lower()
                        if val and key not in all_qc_data[sku_val]:
                            all_qc_data[sku_val][key] = str(val).strip()
                print(f"[QC] After {fname}: total SKUs = {len(all_qc_data)}")
            except Exception as e:
                print(f"[QC] Error processing {fname}: {e}")
                import traceback; traceback.print_exc()

    print(f"[QC] All QC data loaded. SKUs: {list(all_qc_data.keys())[:10]}")

    if all_qc_data and 'SKU Id' in df.columns:
        for field in LABEL_FIELD_MAP.values():
            if field not in df.columns:
                df[field] = ''

        for idx, row in df.iterrows():
            sku_id = str(row['SKU Id']).strip().lower()
            if sku_id not in all_qc_data:
                continue
            qdata = all_qc_data[sku_id]
            for col_name in df.columns:
                if col_name == 'SKU Id':
                    continue
                current_val = df.at[idx, col_name]
                if pd.notna(current_val) and str(current_val).strip() and str(current_val).strip().upper() != 'N/A':
                    continue
                raw_val = qdata.get(col_name.lower(), '')
                if not raw_val or raw_val.upper() == 'N/A':
                    col_lower = col_name.lower()
                    for qkey, qval in qdata.items():
                        mapped = LABEL_FIELD_MAP.get(qkey) or LABEL_FIELD_MAP_LOWER.get(qkey)
                        if not mapped and qkey.startswith(MRP_FIELD_PREFIX):
                            mapped = 'MRP'
                        if mapped == col_name and qval and qval.upper() != 'N/A':
                            raw_val = qval
                            break
                if raw_val and raw_val.upper() != 'N/A':
                    df.at[idx, col_name] = raw_val

    existing_cols = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in existing_cols]

    for col in missing:
        df[col] = ''

    for col in REQUIRED_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(object)

    for idx, row in df.iterrows():
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                continue
            val = df.at[idx, col]
            if pd.isna(val) or str(val).strip() == '' or str(val).strip().upper() == 'N/A':
                df.at[idx, col] = compute_dynamic_defaults(row, col)
        if 'Size' in df.columns:
            sv = row.get('Size', '')
            if pd.isna(sv) or str(sv).strip() == '':
                df.at[idx, 'Size'] = 'medium'

    return missing

def take_barcode_screenshots(fsn_pdf, fsn_sku_map, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    doc = fitz.open(fsn_pdf)
    barcode_mapping = {}

    pad_x = 10
    pad_y = 5

    for page in doc:
        blocks = page.get_text("blocks")
        images_info = page.get_image_info()

        # Precompute ALL FSN text positions on this page for row separation
        all_fsn_rects = []
        for fkey in fsn_sku_map:
            hits = page.search_for(fkey)
            for h in hits:
                all_fsn_rects.append(h)

        for fsn, sku in fsn_sku_map.items():
            fsn_instances = page.search_for(fsn)
            if not fsn_instances:
                continue

            fsn_rect = fsn_instances[0]

            img_rect = None
            for img in images_info:
                rect = fitz.Rect(img["bbox"])
                if rect.y1 <= fsn_rect.y0 + 15 and abs(((rect.x0 + rect.x1)/2) - ((fsn_rect.x0 + fsn_rect.x1)/2)) < 45:
                    has_other_fsn_between = any(
                        rect.y0 < other.y0 < fsn_rect.y0
                        for other in all_fsn_rects
                        if other != fsn_rect
                    )
                    if not has_other_fsn_between:
                        img_rect = rect
                        break

            bottom_rect = fsn_rect
            for b in blocks:
                text_content = b[4]
                if sku.lower() in text_content.lower() and b[1] > fsn_rect.y1 - 5 and b[1] < fsn_rect.y1 + 50:
                    bottom_rect = fitz.Rect(b[:4])
                    break

            crop_x0 = (img_rect.x0 - pad_x) if img_rect else (fsn_rect.x0 - 20)
            crop_x1 = (img_rect.x1 + pad_x) if img_rect else (fsn_rect.x1 + 20)
            crop_y0 = (img_rect.y0 - pad_y) if img_rect else (fsn_rect.y0 - pad_y)
            crop_y1 = bottom_rect.y1 + pad_y

            final_rect = fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)

            mat = fitz.Matrix(8.0, 8.0)
            pix = page.get_pixmap(matrix=mat, clip=final_rect)

            img_path = os.path.join(output_dir, f"{sku}_screenshot.png")
            pix.save(img_path)
            barcode_mapping[sku] = img_path

    doc.close()
    return barcode_mapping

def generate_labels(csv_path, fsn_pdf_path):
    df = pd.read_csv(csv_path)

    fsn_sku_map = {str(row['FSN']).strip(): str(row['SKU Id']).strip().lower() for _, row in df.iterrows()}

    tmp_dir = tempfile.mkdtemp()
    barcode_map = take_barcode_screenshots(fsn_pdf_path, fsn_sku_map, tmp_dir)

    label_width = 4 * inch
    label_height = 6 * inch

    output_path = os.path.join(tempfile.gettempdir(), f"Final_SKU_Labels_{uuid.uuid4().hex[:8]}.pdf")
    c = canvas.Canvas(output_path, pagesize=(label_width, label_height))

    size_column = None
    for col in df.columns:
        if col.strip().lower() == 'size':
            size_column = col
            break

    dim_column = None
    possible_dim_names = ['Dimensions (cm)', 'Dimensions(cm)', 'Dimensions_cm', 'Dimensions', 'Dim (cm)', 'Dim', 'Dimension']
    for col in df.columns:
        col_clean = col.strip()
        if col_clean in possible_dim_names:
            dim_column = col
            break
    if not dim_column:
        for col in df.columns:
            col_clean = col.strip().lower()
            if 'dimension' in col_clean or 'dim' in col_clean:
                dim_column = col
                break

    poster_col = None
    for col in df.columns:
        if col.strip().lower() == 'poster code':
            poster_col = col
            break

    for _, row in df.iterrows():
        model_number = str(row['SKU Id']).strip().lower()
        barcode_img_path = barcode_map.get(model_number)
        generic_name = str(row['Generic Name']).strip().lower() if pd.notna(row['Generic Name']) else ''
        is_poster = 'poster' in generic_name

        for _ in range(int(row['Quantity Sent'])):
            margin_left = 0.2 * inch
            margin_top = 0.3 * inch
            margin_bottom = 0.2 * inch
            available_width = label_width - 2 * margin_left
            line_spacing = 0.3 * inch

            current_y = label_height - margin_top

            c.setFont("Helvetica", 12)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(margin_left, current_y, f"model_number-{model_number}")
            current_y -= line_spacing

            if is_poster:
                title_col = None
                for col in df.columns:
                    if col.strip().lower() == 'product name':
                        title_col = col
                        break
                if title_col:
                    raw_title = str(row[title_col]).strip() if pd.notna(row[title_col]) else ''
                    if raw_title:
                        c.setFont("Helvetica", 8)
                        title_text = f"title- {raw_title}"
                        if c.stringWidth(title_text, "Helvetica", 8) > available_width:
                            while c.stringWidth(title_text + "...", "Helvetica", 8) > available_width and len(title_text) > 7:
                                title_text = title_text[:-1]
                            title_text = title_text + "..."
                        c.drawString(margin_left, current_y, title_text)
                        c.setFont("Helvetica", 12)
                        current_y -= 0.25 * inch

                if poster_col:
                    poster_code = str(row[poster_col]).strip() if pd.notna(row[poster_col]) else ''
                    if poster_code:
                        c.setFont("Helvetica", 8)
                        c.drawString(margin_left, current_y, f"poster_code- {poster_code}")
                        c.setFont("Helvetica", 12)
                        current_y -= 0.25 * inch

            c.drawString(margin_left, current_y, f"brand- {row['Brand']}")
            current_y -= line_spacing

            c.drawString(margin_left, current_y, f"Net Quantity - {row['Net Quantity']}")
            current_y -= line_spacing

            size_value = row[size_column] if size_column and pd.notna(row[size_column]) and str(row[size_column]).strip() else 'medium'
            c.drawString(margin_left, current_y, f"Size - {size_value}")
            current_y -= line_spacing

            dim_value = row[dim_column] if dim_column and pd.notna(row[dim_column]) else ''
            c.drawString(margin_left, current_y, f"Dimensions (cm) - {dim_value}")
            current_y -= line_spacing

            c.drawString(margin_left, current_y, f"MRP Rs.{row['MRP']}.00 (Inclusive of all taxes)")
            current_y -= line_spacing

            c.drawString(margin_left, current_y, f"Generic Name- {row['Generic Name']}")
            current_y -= line_spacing

            mfg_text = f"Month & Year of Manufacturing- {normalize_mfg_date(row['Month & Year of Manufacturing'])}"
            mfg_font_size = 12
            while mfg_font_size > 7 and c.stringWidth(mfg_text, "Helvetica", mfg_font_size) > available_width:
                mfg_font_size -= 1
            c.setFont("Helvetica", mfg_font_size)
            c.drawString(margin_left, current_y, mfg_text)
            c.setFont("Helvetica", 12)
            current_y -= line_spacing

            manufacturer = str(row['Manufactured by / Marketed by'])
            c.drawString(margin_left, current_y, "Manufactured by / Marketed by-")
            current_y -= line_spacing
            c.drawString(margin_left, current_y, manufacturer)
            current_y -= 0.35 * inch

            care_details = str(row['Customer Care Details'])
            c.drawString(margin_left, current_y, "Customer Care Details-")
            current_y -= line_spacing
            c.drawString(margin_left, current_y, care_details)
            current_y -= 0.35 * inch

            c.setFont("Helvetica", 10)
            c.drawString(margin_left, current_y, "EAN/FSN/LID Barcode")
            current_y -= 0.3 * inch

            if barcode_img_path and os.path.exists(barcode_img_path):
                barcode_available_height = current_y - margin_bottom
                barcode_height = min(barcode_available_height, 1.5 * inch)
                barcode_width = label_width - (2 * margin_left)
                c.drawImage(
                    barcode_img_path,
                    margin_left,
                    current_y - barcode_height,
                    width=barcode_width,
                    height=barcode_height,
                    preserveAspectRatio=True
                )

            c.showPage()

    c.save()

    # Cleanup temp barcode images
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path

def read_spreadsheet(filepath, header=None, skiprows=None):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.csv':
        kwargs = {'on_bad_lines': 'skip'}
        if skiprows is not None:
            kwargs['skiprows'] = skiprows
        if header is not None:
            kwargs['header'] = header
        try:
            return pd.read_csv(filepath, **kwargs)
        except Exception:
            kwargs['sep'] = ';'
            return pd.read_csv(filepath, **kwargs)
    kwargs = {}
    if header is not None:
        kwargs['header'] = header
    if skiprows is not None:
        kwargs['skiprows'] = skiprows
    return pd.read_excel(filepath, **kwargs)

def find_header_row(filepath, marker):
    ext = os.path.splitext(filepath)[1].lower()
    marker_lower = marker.strip().lower()
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        for line_num, line in enumerate(f):
            if marker_lower in line.strip().lower():
                return line_num
    return None

def process_meesho(filepath):
    header_row_idx = find_header_row(filepath, MEESHO_HEADER_MARKER)
    if header_row_idx is None:
        raise ValueError(f"Could not find header row containing '{MEESHO_HEADER_MARKER}'")

    df = read_spreadsheet(filepath, header=0, skiprows=header_row_idx)
    df.columns = df.columns.str.strip()

    source_cols = [c for c in MEESHO_COLUMNS_CONFIG.values() if c in df.columns]
    missing_cols = [c for c in MEESHO_COLUMNS_CONFIG.values() if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing expected columns in Meesho file: {', '.join(missing_cols)}")

    for filter_col, filter_val in MEESHO_FILTERS.items():
        if filter_col in df.columns:
            df = df[df[filter_col].astype(str).str.strip() == filter_val]

    output_cols = list(MEESHO_COLUMNS_CONFIG.keys())
    source_to_output = {v: k for k, v in MEESHO_COLUMNS_CONFIG.items()}
    result = df[list(MEESHO_COLUMNS_CONFIG.values())].rename(columns=source_to_output)
    result = result.reset_index(drop=True)
    return result

def process_flipkart(filepath):
    header_row_idx = find_header_row(filepath, FLIPKART_HEADER_MARKER)
    if header_row_idx is None:
        raise ValueError(f"Could not find header row containing '{FLIPKART_HEADER_MARKER}'")

    df = read_spreadsheet(filepath, header=0, skiprows=header_row_idx)
    df.columns = df.columns.str.strip()

    missing_cols = [c for c in FLIPKART_COLUMNS_CONFIG.values() if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing expected columns in Flipkart file: {', '.join(missing_cols)}")

    for filter_col, filter_val in FLIPKART_FILTERS.items():
        if filter_col in df.columns:
            df = df[df[filter_col].astype(str).str.strip() == filter_val]

    source_to_output = {v: k for k, v in FLIPKART_COLUMNS_CONFIG.items()}
    result = df[list(FLIPKART_COLUMNS_CONFIG.values())].rename(columns=source_to_output)
    result = result.reset_index(drop=True)
    return result

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate():
    if 'consignment' not in request.files or 'fsn' not in request.files:
        return jsonify({'error': 'Both files are required'}), 400

    consignment = request.files['consignment']
    fsn = request.files['fsn']

    if not consignment.filename or not fsn.filename:
        return jsonify({'error': 'Both files are required'}), 400

    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, consignment.filename)
    fsn_path = os.path.join(tmp_dir, fsn.filename)

    consignment.save(csv_path)
    fsn.save(fsn_path)

    try:
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()
        df.drop(columns=[c for c in UNNECESSARY_COLUMNS if c in df.columns], inplace=True)

        qc_files = request.files.getlist('qc')
        labels_file = request.files.get('labels')
        fill_qc_and_defaults(df, labels_file, qc_files, tmp_dir)
        df.to_csv(csv_path, index=False)

        if 'Brand' not in df.columns:
            return jsonify({'error': 'Brand column is missing from the consignment file'}), 400

        empty_brands = [str(row.get('SKU Id', f'Row {i+2}')).strip()
                        for i, row in df.iterrows()
                        if pd.isna(row.get('Brand')) or str(row.get('Brand', '')).strip() == '']
        if empty_brands:
            return jsonify({
                'error': f'Brand is empty for the following SKUs: {", ".join(empty_brands)}. '
                         f'Please fill all Brand cells before generating labels.'
            }), 400

        if 'MRP' in df.columns:
            empty_mrps = [str(row.get('SKU Id', f'Row {i+2}')).strip()
                          for i, row in df.iterrows()
                          if pd.isna(row.get('MRP')) or str(row.get('MRP', '')).strip() == '']
            if empty_mrps:
                return jsonify({
                    'error': f'MRP is empty for the following SKUs: {", ".join(empty_mrps)}. '
                             f'Please fill all MRP cells before generating labels.'
                }), 400

        output_path = generate_labels(csv_path, fsn_path)
        return send_file(output_path, as_attachment=True, download_name='z_Final_SKU_Labels.pdf')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.route('/check-columns', methods=['POST'])
def check_columns():
    if 'consignment' not in request.files:
        return jsonify({'error': 'Consignment file is required'}), 400

    consignment = request.files['consignment']
    if not consignment.filename:
        return jsonify({'error': 'Consignment file is required'}), 400

    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, consignment.filename)

    try:
        consignment.save(csv_path)
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()
        df.drop(columns=[c for c in UNNECESSARY_COLUMNS if c in df.columns], inplace=True)

        # --- Read QC folder and fill data ---
        qc_files = request.files.getlist('qc')
        labels_file = request.files.get('labels')
        missing = fill_qc_and_defaults(df, labels_file, qc_files, tmp_dir)

        # --- Check if all required data is present ---
        if not missing and 'Brand' in df.columns:
            empty_brands = [str(row.get('SKU Id', f'Row {i+2}')).strip()
                            for i, row in df.iterrows()
                            if pd.isna(row.get('Brand')) or str(row.get('Brand', '')).strip() == '']
            if empty_brands:
                return jsonify({
                    'error': f'Brand is empty for the following SKUs: {", ".join(empty_brands)}. '
                             f'Please fill all Brand cells before generating labels.'
                }), 400

        output_path = os.path.join(tmp_dir, 'Consignment_Details_Updated.csv')
        df.to_csv(output_path, index=False)

        return send_file(output_path, as_attachment=True,
                         download_name='z_Consignment_Details_Updated.csv',
                         mimetype='text/csv')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.route('/generate-boxes', methods=['POST'])
def generate_boxes():
    if 'consignment' not in request.files:
        return jsonify({'error': 'Consignment file is required'}), 400

    consignment = request.files['consignment']
    if not consignment.filename:
        return jsonify({'error': 'Consignment file is required'}), 400

    tmp_dir = tempfile.mkdtemp()
    csv_path = os.path.join(tmp_dir, consignment.filename)

    try:
        consignment.save(csv_path)
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()

        box_size = 20
        output_rows = []
        box_number = 1

        remainders = []

        for _, row in df.iterrows():
            fsn = str(row['FSN']).strip()
            sku = str(row['SKU Id']).strip()
            qty = int(row['Quantity Sent'])

            full_boxes = qty // box_size
            for _ in range(full_boxes):
                box_name = f"{box_number}_{sku}({box_size})"
                output_rows.append({
                    'BOX NUMBER': box_number,
                    'BOX NAME': box_name,
                    'LENGTH (cm)': 54,
                    'BREADTH (cm)': 40,
                    'HEIGHT (cm)': 35,
                    'WEIGHT (kg)': 12,
                    'NOMINAL VALUE (INR)': 600,
                    'FSN': fsn,
                    'QUANTITY': box_size,
                })
                box_number += 1

            remainder = qty % box_size
            if remainder > 0:
                for _ in range(remainder):
                    remainders.append({'fsn': fsn, 'sku': sku})

        for i in range(0, len(remainders), box_size):
            box_units = remainders[i:i+box_size]
            counts = {}
            for u in box_units:
                key = (u['fsn'], u['sku'])
                counts[key] = counts.get(key, 0) + 1

            name_parts = [f"{s}({q})" for (f, s), q in counts.items()]
            box_name = f"{box_number}_{''.join(name_parts)}"

            for (fsn, sku), qty in counts.items():
                output_rows.append({
                    'BOX NUMBER': box_number,
                    'BOX NAME': box_name,
                    'LENGTH (cm)': 54,
                    'BREADTH (cm)': 40,
                    'HEIGHT (cm)': 35,
                    'WEIGHT (kg)': 12,
                    'NOMINAL VALUE (INR)': 600,
                    'FSN': fsn,
                    'QUANTITY': qty,
                })
            box_number += 1

        out_df = pd.DataFrame(output_rows)
        output_path = os.path.join(tmp_dir, 'Generated_Box_Details.csv')
        out_df.to_csv(output_path, index=False)

        return send_file(output_path, as_attachment=True,
                         download_name='z_Generated_Box_Details.csv',
                         mimetype='text/csv')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

def clean_id(value):
    if pd.isna(value):
        return ''
    s = str(value).strip()
    if s.endswith('.0') and s[:-2].isdigit():
        s = s[:-2]
    if s.lower() == 'nan':
        return ''
    return s

def find_column(df, candidates):
    clean = {c.strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in clean:
            return clean[cand.lower()]
    return None

INVENTORY_COLUMNS = {
    'Warehouse Id': ['Warehouse Id', 'Warehouse ID', 'Warehouse_Id', 'WarehouseID', 'WH Id'],
    'SKU': ['SKU', 'SKU Id', 'SKU ID', 'SKU_Id', 'SKUID'],
    'Live on Website': ['Live on Website', 'Live On Website', 'Live on website', 'Live_on_Website', 'LiveOnWebsite'],
    'Sales 7D': ['Sales 7D', 'Sales 7d', 'Sales 7 Days', 'Sales7D', 'Sales_7D', 'Sales 7D Days'],
}

QUANTITY_PASSWORD = os.environ.get('QUANTITY_RULES_PASSWORD', '200274')
QUANTITY_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quantity_rules.json')

DEFAULT_QUANTITY_RULES = [
    {'live_op': '>', 'live_val': 2, 'sales_op': '>', 'sales_val': 0, 'output': 'formula', 'multiplier': 4, 'constant': 0},
    {'live_op': '>', 'live_val': 2, 'sales_op': '=', 'sales_val': 0, 'output': 'constant', 'multiplier': 0, 'constant': 0},
    {'live_op': '<=', 'live_val': 2, 'sales_op': '>', 'sales_val': 0, 'output': 'formula', 'multiplier': 7, 'constant': 0},
    {'live_op': '<=', 'live_val': 2, 'sales_op': '=', 'sales_val': 0, 'output': 'constant', 'multiplier': 0, 'constant': 20},
]

def load_quantity_rules():
    if os.path.exists(QUANTITY_RULES_PATH):
        try:
            with open(QUANTITY_RULES_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            rules = data.get('rules')
            if isinstance(rules, list) and rules:
                return rules
        except Exception as e:
            print(f"[RULES] Failed to load rules file: {e}")
    return [dict(r) for r in DEFAULT_QUANTITY_RULES]

def save_quantity_rules(rules):
    with open(QUANTITY_RULES_PATH, 'w', encoding='utf-8') as f:
        json.dump({'rules': rules}, f, indent=2)

def apply_op(value, op, target):
    if op == '>':
        return value > target
    if op == '>=':
        return value >= target
    if op == '<':
        return value < target
    if op == '<=':
        return value <= target
    if op == '=':
        return value == target
    if op == '!=':
        return value != target
    return False

def evaluate_quantity_required(rules, live, sales, qty):
    for rule in rules:
        try:
            live_ok = apply_op(live, rule.get('live_op', '>'), float(rule.get('live_val', 2)))
            sales_ok = apply_op(sales, rule.get('sales_op', '='), float(rule.get('sales_val', 0)))
            if live_ok and sales_ok:
                if rule.get('output') == 'formula':
                    return round(float(rule.get('multiplier', 0)) * sales - live - qty)
                return int(rule.get('constant', 0))
        except (TypeError, ValueError):
            continue
    return 0

@app.route('/inventory-warehouses', methods=['POST'])
def inventory_warehouses():
    inv = request.files.get('inventory')
    if not inv or not inv.filename:
        return jsonify({'error': 'Current Inventory file is required'}), 400

    tmp_dir = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp_dir, 'inventory.csv')
        inv.save(path)
        df = pd.read_csv(path)
        df.columns = df.columns.str.strip()

        wh_col = find_column(df, INVENTORY_COLUMNS['Warehouse Id'])
        if not wh_col:
            return jsonify({'error': 'Warehouse Id column not found in the inventory file'}), 400

        warehouses = [clean_id(x) for x in df[wh_col].unique()]
        warehouses = sorted({w for w in warehouses if w != ''})
        return jsonify({'warehouses': warehouses})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.route('/compile-consignment', methods=['POST'])
def compile_consignment():
    files = [f for f in request.files.getlist('consignment') if f and f.filename]
    if not files:
        return jsonify({'error': 'At least one Consignment Details file is required'}), 400

    tmp_dir = tempfile.mkdtemp()
    frames = []
    errors = []
    try:
        for f in files:
            path = os.path.join(tmp_dir, os.path.basename(f.filename).replace('/', '_').replace('\\', '_'))
            f.save(path)
            try:
                df = pd.read_csv(path)
            except Exception:
                df = pd.read_csv(path, sep=None, engine='python')
            df.columns = df.columns.str.strip()

            fsn_col = find_column(df, ['FSN', 'FSN Id', 'FSN_ID', 'FSNID'])
            sku_col = find_column(df, ['SKU Id', 'SKU ID', 'SKU_Id', 'SKUID', 'SKU'])
            qty_col = find_column(df, ['Quantity Sent', 'Quantity', 'Quantity_Sent'])

            missing = []
            if not fsn_col:
                missing.append('FSN')
            if not sku_col:
                missing.append('SKU Id')
            if not qty_col:
                missing.append('Quantity Sent')
            if missing:
                errors.append(f"{f.filename}: missing columns {', '.join(missing)}")
                continue

            sub = pd.DataFrame({
                'FSN': df[fsn_col].map(clean_id),
                'SKU Id': df[sku_col].map(clean_id),
                'Quantity Sent': pd.to_numeric(df[qty_col], errors='coerce'),
            })
            sub = sub[(sub['FSN'] != '') & (sub['SKU Id'] != '')]
            frames.append(sub)

        if not frames:
            raise ValueError('; '.join(errors) if errors else 'No valid consignment data found')

        combined = pd.concat(frames, ignore_index=True)
        grouped = combined.groupby(['FSN', 'SKU Id'], as_index=False)['Quantity Sent'].sum()
        grouped = grouped.dropna(subset=['Quantity Sent'])
        grouped = grouped.sort_values('Quantity Sent', ascending=False)
        grouped['Quantity Sent'] = grouped['Quantity Sent'].astype(int)

        output = grouped
        output_path = os.path.join(tmp_dir, 'Compiled_Consignment.csv')

        inventory = request.files.get('inventory')
        warehouse = (request.form.get('warehouse') or '').strip()
        if inventory and inventory.filename and warehouse:
            inv_path = os.path.join(tmp_dir, 'inventory.csv')
            inventory.save(inv_path)
            inv_df = pd.read_csv(inv_path)
            inv_df.columns = inv_df.columns.str.strip()

            wh_col = find_column(inv_df, INVENTORY_COLUMNS['Warehouse Id'])
            sku_col = find_column(inv_df, INVENTORY_COLUMNS['SKU'])
            live_col = find_column(inv_df, INVENTORY_COLUMNS['Live on Website'])
            sales_col = find_column(inv_df, INVENTORY_COLUMNS['Sales 7D'])

            missing_inv = []
            if not wh_col:
                missing_inv.append('Warehouse Id')
            if not sku_col:
                missing_inv.append('SKU')
            if not live_col:
                missing_inv.append('Live on Website')
            if not sales_col:
                missing_inv.append('Sales 7D')
            if missing_inv:
                raise ValueError(f"Inventory file missing columns: {', '.join(missing_inv)}")

            inv_df[wh_col] = inv_df[wh_col].map(clean_id)
            inv_df = inv_df[inv_df[wh_col] == warehouse]

            if inv_df.empty:
                raise ValueError(f"No rows found for Warehouse Id '{warehouse}' in the inventory file")

            inv_sub = pd.DataFrame({
                'Warehouse Id': inv_df[wh_col],
                'SKU': inv_df[sku_col].map(clean_id),
                'Live on Website': inv_df[live_col],
                'Sales 7D': inv_df[sales_col],
            })
            inv_sub = inv_sub[inv_sub['SKU'] != '']

            grouped_sku = grouped.rename(columns={'SKU Id': 'SKU'})
            merged = inv_sub.merge(grouped_sku[['SKU', 'FSN', 'Quantity Sent']], on='SKU', how='left')
            merged = merged[['Warehouse Id', 'SKU', 'Live on Website', 'Sales 7D', 'Quantity Sent', 'FSN']]
            merged['Quantity Sent'] = merged['Quantity Sent'].fillna(0).astype(int)

            live_numeric = pd.to_numeric(merged['Live on Website'], errors='coerce').fillna(0)
            sales_numeric = pd.to_numeric(merged['Sales 7D'], errors='coerce').fillna(0)
            qty_sent = merged['Quantity Sent'].astype(float)

            rules = load_quantity_rules()
            merged['Quantity Required'] = [
                evaluate_quantity_required(rules, l, s, q)
                for l, s, q in zip(live_numeric, sales_numeric, qty_sent)
            ]

            merged = merged[['Warehouse Id', 'SKU', 'Live on Website', 'Sales 7D', 'Quantity Sent', 'Quantity Required', 'FSN']]
            merged = merged.sort_values('Quantity Sent', ascending=False)
            output = merged
            output_path = os.path.join(tmp_dir, 'Compiled_Inventory.csv')

        output.to_csv(output_path, index=False)

        return send_file(output_path, as_attachment=True,
                         download_name='z_Compiled_Consignment.csv',
                         mimetype='text/csv')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.route('/quantity-rules', methods=['GET'])
def get_quantity_rules():
    return jsonify({'rules': load_quantity_rules()})

@app.route('/quantity-rules', methods=['POST'])
def save_quantity_rules_route():
    data = request.get_json(silent=True) or {}
    password = str(data.get('password', ''))
    if password != QUANTITY_PASSWORD:
        return jsonify({'error': 'Incorrect password'}), 403

    rules = data.get('rules')
    if not isinstance(rules, list) or not rules:
        return jsonify({'error': 'At least one rule is required'}), 400

    valid_ops = {'>', '>=', '<', '<=', '=', '!='}
    cleaned = []
    for r in rules:
        try:
            cleaned.append({
                'live_op': str(r.get('live_op', '>')) if str(r.get('live_op', '>')) in valid_ops else '>',
                'live_val': float(r.get('live_val', 2)),
                'sales_op': str(r.get('sales_op', '=')) if str(r.get('sales_op', '=')) in valid_ops else '=',
                'sales_val': float(r.get('sales_val', 0)),
                'output': 'formula' if r.get('output') == 'formula' else 'constant',
                'multiplier': float(r.get('multiplier', 0)),
                'constant': float(r.get('constant', 0)),
            })
        except (TypeError, ValueError):
            continue

    if not cleaned:
        return jsonify({'error': 'No valid rules provided'}), 400

    save_quantity_rules(cleaned)
    return jsonify({'ok': True, 'rules': cleaned})

def trim_white_margins(page, clip=None, threshold=240):
    if clip is None:
        clip = page.rect
    mat = fitz.Matrix(3.0, 3.0)
    pix = page.get_pixmap(matrix=mat, clip=clip)
    w, h = pix.width, pix.height
    samples = pix.samples
    stride = pix.stride

    def pixel_is_white(x, y):
        offset = y * stride + x * 3
        r, g, b = samples[offset], samples[offset + 1], samples[offset + 2]
        return r > threshold and g > threshold and b > threshold

    top = 0
    for y in range(h):
        found = False
        for x in range(w):
            if not pixel_is_white(x, y):
                found = True
                break
        if found:
            break
        top = y

    bottom = h - 1
    for y in range(h - 1, -1, -1):
        found = False
        for x in range(w):
            if not pixel_is_white(x, y):
                found = True
                break
        if found:
            break
        bottom = y

    left = 0
    for x in range(w):
        found = False
        for y in range(h):
            if not pixel_is_white(x, y):
                found = True
                break
        if found:
            break
        left = x

    right = w - 1
    for x in range(w - 1, -1, -1):
        found = False
        for y in range(h):
            if not pixel_is_white(x, y):
                found = True
                break
        if found:
            break
        right = x

    scale = 1.0 / 3.0
    return fitz.Rect(
        clip.x0 + max(left * scale - 2, 0),
        clip.y0 + max(top * scale - 2, 0),
        clip.x0 + min(right * scale + 2, clip.width),
        clip.y0 + min(bottom * scale + 2, clip.height),
    )

@app.route('/crop-box-labels', methods=['POST'])
def crop_box_labels():
    if 'boxlabels' not in request.files:
        return jsonify({'error': 'Box labels PDF is required'}), 400

    boxlabels = request.files['boxlabels']
    if not boxlabels.filename:
        return jsonify({'error': 'Box labels PDF is required'}), 400

    tmp_dir = tempfile.mkdtemp()
    pdf_path = os.path.join(tmp_dir, boxlabels.filename)

    try:
        boxlabels.save(pdf_path)
        doc = fitz.open(pdf_path)
        output_doc = fitz.open()

        for page_num in range(len(doc)):
            page = doc[page_num]
            page_rect = page.rect

            box_id_instances = page.search_for("Box ID")

            if not box_id_instances or len(box_id_instances) == 1:
                trimmed = trim_white_margins(page)
                new_page = output_doc.new_page(width=trimmed.width, height=trimmed.height)
                new_page.show_pdf_page(new_page.rect, doc, page_num, clip=trimmed)
                continue

            y_positions = sorted([r.y0 for r in box_id_instances])

            boundaries = [0]
            for i in range(len(y_positions) - 1):
                mid = (y_positions[i] + y_positions[i + 1]) / 2
                boundaries.append(mid)
            boundaries.append(page_rect.height)

            for i in range(len(boundaries) - 1):
                clip = fitz.Rect(0, boundaries[i], page_rect.width, boundaries[i + 1])
                trimmed = trim_white_margins(page, clip=clip)
                new_page = output_doc.new_page(width=trimmed.width, height=trimmed.height)
                new_page.show_pdf_page(new_page.rect, doc, page_num, clip=trimmed)

        output_path = os.path.join(tmp_dir, 'Cropped_Box_Labels.pdf')
        output_doc.save(output_path)
        output_doc.close()
        doc.close()

        return send_file(output_path, as_attachment=True,
                         download_name='z_Cropped_Box_Labels.pdf',
                         mimetype='application/pdf')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@app.route('/extract-returns', methods=['POST'])
def extract_returns():
    meesho_files = request.files.getlist('meesho')

    has_meesho = any(f.filename for f in meesho_files)
    has_flipkart = any(f.filename for f in request.files.getlist('flipkart'))
    if not has_meesho and not has_flipkart:
        return jsonify({'error': 'At least one returns file is required'}), 400

    tmp_dir = tempfile.mkdtemp()

    try:
        meesho_frames = []
        flipkart_frames = []

        for meesho_file in meesho_files:
            if meesho_file and meesho_file.filename:
                meesho_path = os.path.join(tmp_dir, meesho_file.filename)
                meesho_file.save(meesho_path)
                meesho_result = process_meesho(meesho_path)
                meesho_frames.append(meesho_result)

        flipkart_files = request.files.getlist('flipkart')
        for fk_file in flipkart_files:
            if fk_file and fk_file.filename:
                flipkart_path = os.path.join(tmp_dir, fk_file.filename)
                fk_file.save(flipkart_path)
                flipkart_result = process_flipkart(flipkart_path)
                flipkart_frames.append(flipkart_result)

        output_path = os.path.join(tmp_dir, 'Compiled_Returns.xlsx')
        columns = list(MEESHO_COLUMNS_CONFIG.keys())
        blank = pd.DataFrame([[None]*len(columns)], columns=columns)

        def concat_with_blanks(dataframes):
            if not dataframes:
                return pd.DataFrame(columns=columns)
            parts = []
            for i, f in enumerate(dataframes):
                if i > 0:
                    parts.append(blank)
                parts.append(f)
            return pd.concat(parts, ignore_index=True)

        meesho_combined = concat_with_blanks(meesho_frames)
        flipkart_combined = concat_with_blanks(flipkart_frames)

        if not meesho_combined.empty and not flipkart_combined.empty:
            combined = pd.concat([meesho_combined, flipkart_combined], ignore_index=True)
        elif not meesho_combined.empty:
            combined = meesho_combined
        else:
            combined = flipkart_combined

        combined.to_excel(output_path, index=False)

        return send_file(output_path, as_attachment=True,
                         download_name='z_Compiled_Returns.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
