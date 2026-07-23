import pandas as pd
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm, inch
import os
import glob
import re
from calendar import month_name, month_abbr

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

def take_barcode_screenshots(fsn_pdf, fsn_sku_map, output_dir=r"D:\sku labels\temp_barcodes"):
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

            # --- 1. Find Barcode (Top) - belonging to THIS row only ---
            img_rect = None
            for img in images_info:
                rect = fitz.Rect(img["bbox"])
                if rect.y1 <= fsn_rect.y0 + 15 and abs(((rect.x0 + rect.x1)/2) - ((fsn_rect.x0 + fsn_rect.x1)/2)) < 45:
                    # Ensure no other FSN text sits between this image and our FSN
                    has_other_fsn_between = any(
                        rect.y0 < other.y0 < fsn_rect.y0
                        for other in all_fsn_rects
                        if other != fsn_rect
                    )
                    if not has_other_fsn_between:
                        img_rect = rect
                        break
            
            # --- 2. Find Title/SKU line (Bottom) ---
            # Search all blocks for the title line that contains our SKU name
            bottom_rect = fsn_rect
            for b in blocks:
                text_content = b[4]
                # Look for the block that contains the SKU name or part of the title
                if sku.lower() in text_content.lower() and b[1] > fsn_rect.y1 - 5 and b[1] < fsn_rect.y1 + 50:
                    bottom_rect = fitz.Rect(b[:4])
                    break
            
            # --- 3. Crop Calculation ---
            crop_x0 = (img_rect.x0 - pad_x) if img_rect else (fsn_rect.x0 - 20)
            crop_x1 = (img_rect.x1 + pad_x) if img_rect else (fsn_rect.x1 + 20)
            crop_y0 = (img_rect.y0 - pad_y) if img_rect else (fsn_rect.y0 - pad_y)
            crop_y1 = bottom_rect.y1 + pad_y
            
            final_rect = fitz.Rect(crop_x0, crop_y0, crop_x1, crop_y1)
            
            # 🔥 INCREASED RESOLUTION - Changed from 3.0 to 8.0 for much higher quality
            mat = fitz.Matrix(8.0, 8.0) 
            pix = page.get_pixmap(matrix=mat, clip=final_rect)
            
            img_path = os.path.join(output_dir, f"{sku}_screenshot.png")
            pix.save(img_path)
            barcode_mapping[sku] = img_path
                
    doc.close()
    return barcode_mapping

def generate_labels():
    base_dir = r"D:\sku labels"
    csv_files = glob.glob(os.path.join(base_dir, "Consignment_Details_*.csv"))
    fsn_files = glob.glob(os.path.join(base_dir, "fsn_label_*.pdf"))
    
    if not csv_files or not fsn_files:
        print("❌ ERROR: Required files not found.")
        return
        
    df = pd.read_csv(csv_files[0])
    
    # Print all column names for debugging
    print("📊 Available columns in CSV:")
    for i, col in enumerate(df.columns):
        print(f"  {i+1}. '{col}'")
    
    fsn_sku_map = {str(row['FSN']).strip(): str(row['SKU Id']).strip().lower() for _, row in df.iterrows()}
    
    print("📸 Taking screenshots with extended title-line capture and HIGH RESOLUTION...")
    barcode_map = take_barcode_screenshots(fsn_files[0], fsn_sku_map)
    
    # 4x6 inch label size
    label_width = 4 * inch
    label_height = 6 * inch
    
    c = canvas.Canvas(os.path.join(base_dir, "Final_SKU_Labels.pdf"), pagesize=(label_width, label_height))
    
    # Find Size column - try exact match first, then case-insensitive
    size_column = None
    for col in df.columns:
        if col.strip().lower() == 'size':
            size_column = col
            break
    
    # Find Dimensions column - try exact match first, then various formats
    dim_column = None
    possible_dim_names = ['Dimensions (cm)', 'Dimensions(cm)', 'Dimensions_cm', 'Dimensions', 'Dim (cm)', 'Dim', 'Dimension']
    for col in df.columns:
        col_clean = col.strip()
        if col_clean in possible_dim_names:
            dim_column = col
            break
    # If still not found, try case-insensitive
    if not dim_column:
        for col in df.columns:
            col_clean = col.strip().lower()
            if 'dimension' in col_clean or 'dim' in col_clean:
                dim_column = col
                break
    
    print(f"📊 Found Size column: '{size_column}'")
    print(f"📊 Found Dimensions column: '{dim_column}'")
    
    # Sample data check
    if dim_column:
        sample_value = df[dim_column].iloc[0] if len(df) > 0 else 'No data'
        print(f"📊 Sample Dimensions value: '{sample_value}'")
    
    for _, row in df.iterrows():
        model_number = str(row['SKU Id']).strip().lower()
        barcode_img_path = barcode_map.get(model_number)
        
        for _ in range(int(row['Quantity Sent'])):
            # Margins
            margin_left = 0.2 * inch
            margin_top = 0.3 * inch
            margin_bottom = 0.2 * inch
            
            # Start from top
            current_y = label_height - margin_top
            
            # --- Model Number (top line) ---
            c.setFont("Helvetica", 14)
            c.setFillColorRGB(0, 0, 0)
            model_text = f"model_number-{model_number}"
            c.drawString(margin_left, current_y, model_text)
            current_y -= 0.35 * inch
            
            # --- Brand ---
            brand_text = f"brand- {row['Brand']}"
            c.drawString(margin_left, current_y, brand_text)
            current_y -= 0.35 * inch
            
            # --- Net Quantity ---
            net_qty_text = f"Net Quantity - {row['Net Quantity']}"
            c.drawString(margin_left, current_y, net_qty_text)
            current_y -= 0.35 * inch
            
            # --- Size --- (always print)
            if size_column:
                size_value = row[size_column] if pd.notna(row[size_column]) else ''
            else:
                size_value = ''
            size_text = f"Size - {size_value}"
            c.drawString(margin_left, current_y, size_text)
            current_y -= 0.35 * inch
            
            # --- Dimensions (cm) --- (always print)
            if dim_column:
                dim_value = row[dim_column] if pd.notna(row[dim_column]) else ''
            else:
                dim_value = ''
            dim_text = f"Dimensions (cm) - {dim_value}"
            c.drawString(margin_left, current_y, dim_text)
            current_y -= 0.35 * inch
            
            # --- MRP ---
            mrp_text = f"MRP Rs.{row['MRP']}.00 (Inclusive of all taxes)"
            c.drawString(margin_left, current_y, mrp_text)
            current_y -= 0.35 * inch
            
            # --- Generic Name ---
            generic_text = f"Generic Name- {row['Generic Name']}"
            c.drawString(margin_left, current_y, generic_text)
            current_y -= 0.35 * inch
            
            # --- Manufacturing Date ---
            mfg_date_text = f"Month & Year of Manufacturing- {normalize_mfg_date(row['Month & Year of Manufacturing'])}"
            mfg_font_size = 14
            available_width = label_width - 2 * margin_left
            while mfg_font_size > 8 and c.stringWidth(mfg_date_text, "Helvetica", mfg_font_size) > available_width:
                mfg_font_size -= 1
            c.setFont("Helvetica", mfg_font_size)
            c.drawString(margin_left, current_y, mfg_date_text)
            c.setFont("Helvetica", 14)
            current_y -= 0.35 * inch
            
            # --- Manufactured by (2 lines) ---
            manufacturer = str(row['Manufactured by / Marketed by'])
            mfg_text_line1 = f"Manufactured by / Marketed by-"
            c.drawString(margin_left, current_y, mfg_text_line1)
            current_y -= 0.35 * inch
            c.drawString(margin_left, current_y, manufacturer)
            current_y -= 0.45 * inch  # Extra space after 2 lines
            
            # --- Customer Care Details (2 lines) ---
            care_details = str(row['Customer Care Details'])
            care_text_line1 = f"Customer Care Details-"
            c.drawString(margin_left, current_y, care_text_line1)
            current_y -= 0.35 * inch
            c.drawString(margin_left, current_y, care_details)
            current_y -= 0.45 * inch  # Extra space after 2 lines
            
            # --- Barcode Label ---
            c.setFont("Helvetica", 12)
            c.drawString(margin_left, current_y, "EAN/FSN/LID Barcode")
            current_y -= 0.3 * inch
            
            # --- Barcode Image ---
            if barcode_img_path and os.path.exists(barcode_img_path):
                # Calculate remaining space for barcode
                barcode_available_height = current_y - margin_bottom
                
                # Make barcode as large as possible within remaining space
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
            
            # Add page break
            c.showPage()
            
    c.save()
    print("✅ Successfully generated: Final_SKU_Labels.pdf")
    print(f"📄 Each label includes Size and Dimensions (cm) lines with HIGH RESOLUTION barcode")

if __name__ == "__main__":
    generate_labels()