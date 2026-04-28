import os
from fpdf import FPDF

# Define the output path for the test PDF
test_report_dir = "reports"
test_filename = "test_output.pdf"
test_report_path = os.path.join(test_report_dir, test_filename)

def run_test_pdf_creation():
    print(f"Attempting to create a test PDF at: {test_report_path}")

    # Ensure the reports directory exists
    os.makedirs(test_report_dir, exist_ok=True)
    print(f"Directory '{test_report_dir}' exists or was created.")

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)

    # Try to add some basic text
    try:
        pdf.cell(0, 10, "Hello, this is a test PDF.", ln=True)
        pdf.cell(0, 10, "If you see this, FPDF is working!", ln=True)
        print("Added text to PDF.")
    except Exception as e:
        print(f"ERROR: Failed to add text to PDF: {e}")
        return # Stop if adding text fails

    # Attempt to output the PDF
    try:
        pdf.output(test_report_path)
        print(f"SUCCESS: Test PDF created at: {test_report_path}")
        # Verify file existence and size
        if os.path.exists(test_report_path) and os.path.getsize(test_report_path) > 0:
            print("SUCCESS: File exists and is not empty.")
        else:
            print("FAILURE: File was not created or is empty after pdf.output().")
    except Exception as e:
        print(f"ERROR: FPDF output failed: {e}")

if __name__ == '__main__':
    run_test_pdf_creation()