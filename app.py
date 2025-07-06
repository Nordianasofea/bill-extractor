import os
import re
import tempfile
import logging
from datetime import datetime
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, send_file
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path
import pytesseract
import pandas as pd
import traceback
import cv2
import numpy as np

# --- CONFIGURATION ---
# If Tesseract or Poppler are not in your system's PATH, specify their locations here.
# Uncomment and modify the relevant line if needed.

# For Windows:
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
# POPPLER_PATH = r"C:\path\to\poppler-xx.x.x\bin"

# For macOS/Linux:
# pytesseract.pytesseract.tesseract_cmd = r'/usr/local/bin/tesseract'
# ---------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB limit

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    """Checks if the file format is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def clean_numeric(value):
    """
    FIXED: Cleans and converts a string value to a float, correctly handling negative values.
    The original function removed the '-' sign, turning discounts into positive numbers.
    """
    if value is None:
        return 0.0
    
    val_str = str(value).replace('RM', '').replace(',', '').strip()
    is_negative = val_str.endswith('-')
    
    # Remove the trailing hyphen for conversion
    if is_negative:
        val_str = val_str[:-1]
        
    try:
        number = float(val_str)
        return -number if is_negative else number
    except (ValueError, TypeError):
        return 0.0

def extract_data_from_text(text):
    """
    CORRECTED VERSION: Fixes issues with MD (RM) extraction and Amount (RM) calculation
    - Makes the "MD (RM)" regex more robust by handle OCR errors (e.g., "ehendak").
    - Corrects the "Amount (RM)" calculation logic to ensure it's always the sum of Total and MD.
    - MODIFIED: Implements a MORE PRECISE fallback regex for "Kehendak maksima RM".
    """
    logger.info("Starting data extraction from OCR text...")
    
    # Print the full OCR text to the terminal for debugging
    print("\n" + "="*20 + " IDENTIFIED OCR TEXT " + "="*20)
    print(text)
    print("="*60 + "\n")

    def find_value(pattern, text_block):
        """Helper function to find a value using regex."""
        match = re.search(pattern, text_block, re.IGNORECASE | re.DOTALL)
        if match:
            for group in match.groups():
                if group:
                    return group.strip().replace('\n', ' ')
        return None

    # Definitive patterns, with "Kehendak maksima RM" removed to be handled separately.
    patterns = {
        "Tarikh bill": r"Tarikh Bil\s*:?\s*([\d\.]+)",
        "kegunaan kWh": r"Kegunaan\s+([\d,]+\.\d*)\s+[\d\.]+\s+[\d,]+\.\d*",
        "kegunaan RM": r"Kegunaan\s+[\d,]+\.\d*\s+[\d\.]+\s+([\d,]+\.\d*)",
        "Kehendak maksima kWh": r"K?ehendak Maksima\s+([\d,]+\.?\d*)",
        "KWTBB": r'KWTBB.*?RM\s*(\d{1,3}(?:,\d{3})*\.\d{2})',
        "Diskaun": r"Diskaun TNB\s+RM\s+(-?[\d,]+\.\d*-?)",
        "ICPT": r"ICPT\s*\(.*?\)[\s\S]*?RM\s*(-?[\d,]+\.\d*-?)",
        "Caj Semasa": r"Caj Semasa[\s\S]*?([\d,]+\.\d*)",
        "Caj Sambungan Beban": r"Caj Sambungan Beban[\s\S]*?RM\s+([\d,]+\.\d*)"
    }
    
    malay_data = {}
    for key, pattern in patterns.items():
        malay_data[key] = find_value(pattern, text)
        
    # --- Custom Logic for "Kehendak maksima RM" with Fallback ---
    # 1. Try the primary, more specific pattern first.
    md_rm_value = find_value(r"K?ehendak Maksima RM\s+RM\s+([\d,]+\.\d{2})", text)
    
    # 2. If it fails, use the NEW, more precise fallback pattern for the "Blok Tarif" section.
    if not md_rm_value:
        # This pattern now skips the first two numbers to capture the third (the amount).
        fallback_pattern = r"K?ehendak Maksima\s+[\d,]+\.?\d*\s+[\d\.]+\s+([\d,]+\.\d{2})"
        md_rm_value = find_value(fallback_pattern, text)
        
    malay_data["Kehendak maksima RM"] = md_rm_value
    # --- End of Custom Logic ---
    
    # This calculation will now be correct because md_rm_value is being extracted properly.
    total_rm_val = clean_numeric(malay_data.get("kegunaan RM"))
    md_rm_val = clean_numeric(malay_data.get("Kehendak maksima RM"))
    malay_data["Amount (RM)"] = total_rm_val + md_rm_val

    # Key mapping from Malay to English
    key_mapping = {
        "Tarikh bill": "Months",
        "kegunaan kWh": "Total (kWh)",
        "kegunaan RM": "Total (RM)",
        "Kehendak maksima kWh": "MD (kW)",
        "Kehendak maksima RM": "MD (RM)",
        "Amount (RM)": "Amount (RM)",
        "ICPT": "ICPT (RM)",
        "Caj Sambungan Beban": "CLC (RM)",
        "Diskaun": "Discount (RM)",
        "KWTBB": "KWTBB (RM)",
        "Caj Semasa": "Total Bill (RM)"
    }
    
    english_data = {english_key: malay_data.get(malay_key) for malay_key, english_key in key_mapping.items()}
    
    # Add empty fields for consistency
    for key in ['Peak (kWh)', 'Peak (RM)', 'Off-peak (kWh)', 'Off-peak (RM)']:
        english_data[key] = None
        
    logger.info(f"Extracted data (after mapping): {english_data}")
    return english_data


def process_pdf(filepath):
    """
    MODIFIED: Removes the thresholding step from the pre-processing chain.
    The OCR will now process a blurred grayscale image instead of a pure black-and-white one.
    """
    try:
        images = convert_from_path(filepath, dpi=300) 
        custom_config = r'--oem 3 --psm 6 -l msa+eng'
        all_data = []

        for page_num, image in enumerate(images, start=1):
            try:
                # --- START: MODIFIED PRE-PROCESSING CHAIN ---

                # 1. Convert the image from its original format to an OpenCV format
                open_cv_image = np.array(image)
                
                # 2. Convert to Grayscale
                gray_image = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2GRAY)

                # 3. Noise Removal: Use Median Blur to remove digital speckles.
                blurred_image = cv2.medianBlur(gray_image, 3)
                
           
                text = pytesseract.image_to_string(blurred_image, config=custom_config)
                
                page_data = extract_data_from_text(text)
                
                if page_data.get('Months') or page_data.get('Total Bill (RM)'):
                    page_data['Page'] = page_num
                    
                    # Clean all numeric data before adding to the list
                    for key, value in page_data.items():
                        # We only clean keys that are expected to be numeric
                        if any(x in key for x in ['(kWh)', '(kW)', '(RM)']):
                             page_data[key] = clean_numeric(value)

                    all_data.append(page_data)
                    logger.info(f"✅ Data found on page {page_num} using modified processing.")
                else:
                    logger.warning(f"⚠️ Page {page_num} does not contain extractable bill data.")

            except Exception as page_error:
                logger.error(f"❌ Error processing page {page_num}: {page_error}")
                print(traceback.format_exc())

        return all_data

    except Exception as e:
        logger.error(f"PDF processing failed: {str(e)}")
        print(traceback.format_exc())
        raise


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'file' not in request.files:
            return render_template('error.html', error="No file sent. Please select a file.")
        file = request.files['file']
        if file.filename == '' or not allowed_file(file.filename):
            return render_template('error.html', error="Please select a valid PDF file.")

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            extracted_data = process_pdf(filepath)
            if not extracted_data:
                return render_template('error.html', error="No valid bill data could be extracted from the PDF. Check the terminal log for more information.")

            df = pd.DataFrame(extracted_data)
            
            # Column order for the Excel file and HTML table
            columns_order = [
                'Months', 'Peak (kWh)', 'Peak (RM)', 'Off-peak (kWh)', 'Off-peak (RM)',
                'Total (kWh)', 'Total (RM)', 'MD (kW)', 'MD (RM)', 'Amount (RM)',
                'ICPT (RM)', 'CLC (RM)', 'Discount (RM)', 'KWTBB (RM)', 'Total Bill (RM)'
            ]
            
            df = df.reindex(columns=['Page'] + columns_order)

            excel_output = BytesIO()
            with pd.ExcelWriter(excel_output, engine='xlsxwriter') as writer:
                df.to_excel(writer, sheet_name='Bill Data', index=False)
                workbook = writer.book
                worksheet = writer.sheets['Bill Data']
                # Define formats for numbers and currency
                money_format = workbook.add_format({'num_format': 'RM #,##0.00'})
                num_format = workbook.add_format({'num_format': '#,##0.00'})
                date_format = workbook.add_format({'num_format': 'dd/mm/yyyy'})

                # Apply formats to columns for a professional look
                for i, col in enumerate(df.columns):
                    if col == 'Months':
                        worksheet.set_column(i, i, 15, date_format)
                    elif "(RM)" in col:
                        worksheet.set_column(i, i, 18, money_format)
                    elif "(kWh)" in col or "(kW)" in col:
                         worksheet.set_column(i, i, 18, num_format)
                    else: # For Page number etc.
                        worksheet.set_column(i, i, 10)

            excel_output.seek(0)

            # Use a temporary file to store the generated Excel
            with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx', prefix='bill_') as tmp:
                tmp.write(excel_output.getvalue())
                excel_path = tmp.name

            table_html = df.to_html(classes='table table-striped table-hover', index=False, na_rep='N/A', border=0, float_format='{:,.2f}'.format)

            return render_template(
                'results.html',
                table_html=table_html,
                filename=filename,
                num_pages=len(extracted_data),
                excel_path=excel_path, # Pass the temp file path to the template
                now=datetime.now()
            )

        except Exception as e:
            logger.error(f"An error occurred during file processing: {e}")
            print(traceback.format_exc())
            return render_template('error.html', error=str(e))
        finally:
            # Clean up the uploaded file
            if 'filepath' in locals() and os.path.exists(filepath):
                os.remove(filepath)

    return render_template('upload.html')

@app.route('/download-excel')
def download_excel():
    excel_path = request.args.get('path')
    original_filename = request.args.get('filename', 'bill_data.pdf')
    
    if excel_path and os.path.exists(excel_path):
        try:
            # Create a more descriptive download name
            base_filename = os.path.splitext(original_filename)[0]
            download_name = f"{base_filename}_extracted_data.xlsx"
            return send_file(excel_path, as_attachment=True, download_name=download_name)
        finally:
            # IMPORTANT: Clean up the temporary excel file after sending it
            try:
                os.unlink(excel_path)
            except OSError as e:
                logger.error(f"Error deleting temporary file {excel_path}: {e}")
                
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
