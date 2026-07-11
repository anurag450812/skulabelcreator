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
from datetime import datetime, timedelta
from calendar import month_abbr

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

REQUIRED_COLUMNS = [
    'Product Name', 'FSN', 'SKU Id', 'Brand', 'Quantity Sent', 'MRP',
    'Net Quantity', 'Generic Name', 'Month & Year of Manufacturing',
    'Manufactured by / Marketed by', 'Customer Care Details',
    'EAN/FSN/LID Barcode', 'Dimensions (cm)', 'Size'
]

STATIC_DEFAULTS = {
    'Net Quantity': '1 unit',
    'Customer Care Details': 'email us at- xidlzzzzzz@gmail.com',
    'EAN/FSN/LID Barcode': '',
    'Size': 'medium',
}

def get_last_month_str():
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - timedelta(days=1)
    return f"{last_month.day}-{month_abbr[last_month.month]}"

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
        elif '0' in sku_id:
            return '35*6*6 cm'
        return '48*16*3 cm'

    if col == 'Generic Name':
        if '0' in sku_id:
            return 'poster'
        return 'painting'

    if col == 'Month & Year of Manufacturing':
        return get_last_month_str()

    return STATIC_DEFAULTS.get(col, '')

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

    for _, row in df.iterrows():
        model_number = str(row['SKU Id']).strip().lower()
        barcode_img_path = barcode_map.get(model_number)

        for _ in range(int(row['Quantity Sent'])):
            margin_left = 0.2 * inch
            margin_top = 0.3 * inch
            margin_bottom = 0.2 * inch

            current_y = label_height - margin_top

            c.setFont("Helvetica", 14)
            c.setFillColorRGB(0, 0, 0)
            c.drawString(margin_left, current_y, f"model_number-{model_number}")
            current_y -= 0.35 * inch

            c.drawString(margin_left, current_y, f"brand- {row['Brand']}")
            current_y -= 0.35 * inch

            c.drawString(margin_left, current_y, f"Net Quantity - {row['Net Quantity']}")
            current_y -= 0.35 * inch

            size_value = row[size_column] if size_column and pd.notna(row[size_column]) else ''
            c.drawString(margin_left, current_y, f"Size - {size_value}")
            current_y -= 0.35 * inch

            dim_value = row[dim_column] if dim_column and pd.notna(row[dim_column]) else ''
            c.drawString(margin_left, current_y, f"Dimensions (cm) - {dim_value}")
            current_y -= 0.35 * inch

            c.drawString(margin_left, current_y, f"MRP Rs.{row['MRP']}.00 (Inclusive of all taxes)")
            current_y -= 0.35 * inch

            c.drawString(margin_left, current_y, f"Generic Name- {row['Generic Name']}")
            current_y -= 0.35 * inch

            c.drawString(margin_left, current_y, f"Month & Year of Manufacturing- {row['Month & Year of Manufacturing']}")
            current_y -= 0.35 * inch

            manufacturer = str(row['Manufactured by / Marketed by'])
            c.drawString(margin_left, current_y, "Manufactured by / Marketed by-")
            current_y -= 0.35 * inch
            c.drawString(margin_left, current_y, manufacturer)
            current_y -= 0.45 * inch

            care_details = str(row['Customer Care Details'])
            c.drawString(margin_left, current_y, "Customer Care Details-")
            current_y -= 0.35 * inch
            c.drawString(margin_left, current_y, care_details)
            current_y -= 0.45 * inch

            c.setFont("Helvetica", 12)
            c.drawString(margin_left, current_y, "EAN/FSN/LID Barcode")
            current_y -= 0.3 * inch

            if barcode_img_path and os.path.exists(barcode_img_path):
                barcode_available_height = current_y - margin_bottom
                barcode_height = min(barcode_available_height, 2.0 * inch)
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
        output_path = generate_labels(csv_path, fsn_path)
        return send_file(output_path, as_attachment=True, download_name='Final_SKU_Labels.pdf')
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

        # --- Fill MRP from QC folder if present ---
        qc_files = request.files.getlist('qc')
        if qc_files:
            for qc_file in qc_files:
                fname = qc_file.filename or ''
                base_name = os.path.basename(fname).lower()
                if base_name.startswith('quality_check') and base_name.endswith('.csv'):
                    qc_path = os.path.join(tmp_dir, base_name)
                    qc_file.save(qc_path)
                    try:
                        qc_df = pd.read_csv(qc_path)
                        sku_col = None
                        mrp_col = None
                        for col in qc_df.columns:
                            if col.strip().upper() == 'SKU':
                                sku_col = col
                            if col.strip().upper() == 'MRP':
                                mrp_col = col
                        if sku_col and mrp_col:
                            sku_mrp_map = {
                                str(row[sku_col]).strip().lower(): str(row[mrp_col]).strip()
                                for _, row in qc_df.iterrows()
                                if pd.notna(row[sku_col]) and pd.notna(row[mrp_col])
                            }
                            if 'SKU Id' in df.columns:
                                if 'MRP' not in df.columns:
                                    df['MRP'] = ''
                                for idx, row in df.iterrows():
                                    sku_id = str(row['SKU Id']).strip().lower()
                                    mrp_val = row.get('MRP', '')
                                    if sku_id in sku_mrp_map and (pd.isna(mrp_val) or str(mrp_val).strip() == ''):
                                        df.at[idx, 'MRP'] = sku_mrp_map[sku_id]
                    except Exception:
                        pass

        existing_cols = [c.strip() for c in df.columns]
        missing = [c for c in REQUIRED_COLUMNS if c not in existing_cols]

        if not missing:
            return jsonify({'missing': [], 'all_present': True})

        for col in missing:
            df[col] = ''

        for idx, row in df.iterrows():
            for col in missing:
                df.at[idx, col] = compute_dynamic_defaults(row, col)

        output_path = os.path.join(tmp_dir, 'Consignment_Details_Updated.csv')
        df.to_csv(output_path, index=False)

        return send_file(output_path, as_attachment=True,
                         download_name='Consignment_Details_Updated.csv',
                         mimetype='text/csv')
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == '__main__':
    app.run(debug=True, port=5000)
