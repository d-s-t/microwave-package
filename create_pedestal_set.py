import win32com.client
import pythoncom
import os
import shutil
import re
import time
import logging
import argparse
from typing import List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')


def connect_solidworks() -> Optional[object]:
    try:
        logging.info("Connecting to SolidWorks...")
        swApp = win32com.client.Dispatch("SldWorks.Application")
        swApp.Visible = True
        # Set STEP export to AP214
        swApp.SetUserPreferenceIntegerValue(278, 214)
        return swApp
    except Exception as e:
        logging.error("Failed to connect to SolidWorks: %s", e)
        return None


def read_default_file(path: str) -> str:
    with open(path, 'r') as f:
        return f.read()


def write_default_file(path: str, content: str) -> None:
    with open(path, 'w') as f:
        f.write(content)


def open_and_activate_assembly(swApp: object, assembly_path: str) -> Optional[object]:
    try:
        logging.info("Opening assembly: %s", assembly_path)
        arg_err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        arg_warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swModel = swApp.OpenDoc6(assembly_path, 2, 1, "", arg_err, arg_warn)
        logging.info("Activating document...")
        swModel = swApp.ActivateDoc3(assembly_path, False, 2, arg_err)
        if not swModel:
            logging.error("Failed to activate assembly: %s", assembly_path)
            return None
        return swModel
    except Exception as e:
        logging.error("Error opening/activating assembly: %s", e)
        return None


def rebuild_model(swModel: object) -> None:
    logging.info("Rebuilding assembly...")
    try:
        swModel.ForceRebuild3(False)
    except Exception as e:
        logging.warning("Rebuild failed: %s", e)


def save_as_step(swModel: object, step_filepath: str) -> Tuple[bool, Optional[str]]:
    """
    Save the active document as a STEP file.

    Under plain (late-bound) Dispatch, SolidWorks' SaveAs return value and
    its ByRef Errors/Warnings out-params do not marshal back to Python
    reliably (this is a known win32com/SolidWorks quirk, not something you
    can fix by passing lists/VARIANTs differently) -- the file is often
    written correctly even when Python sees `success == False`.

    Rather than trust the return value, we check the file itself:
    delete any pre-existing file with the same name first, call SaveAs,
    then confirm the new file actually exists (and is non-empty).
    """
    if os.path.exists(step_filepath):
        try:
            os.remove(step_filepath)
        except OSError as e:
            return False, f"Could not remove existing file before save: {e}"

    try:
        # The 4th arg (ExportData) is typed as a COM object pointer, and the
        # 5th/6th args (Errors, Warnings) are ByRef longs. Plain Python
        # None/0 don't auto-convert to these under late-bound dispatch and
        # raise "Type mismatch" (-2147352571) -- each must be wrapped
        # explicitly as the right VARIANT type.
        export_data = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        err_var = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warn_var = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        swModel.Extension.SaveAs(step_filepath, 0, 1, export_data, err_var, warn_var)
    except Exception as e:
        return False, f"SaveAs raised an exception: {e}"

    # Give SolidWorks a brief moment to finish flushing the file to disk.
    for _ in range(20):
        if os.path.exists(step_filepath) and os.path.getsize(step_filepath) > 0:
            return True, None
        time.sleep(0.25)

    return False, "SaveAs returned but no (non-empty) STEP file was found on disk"


def process_depth(depth: float, default_txt_path: str, original_content: str, experiment_dir: str, swModel: object) -> None:
    logging.info("Processing pedestal depth: %smm", depth)
    # Replace pedestal_depth value in the default content
    new_content = re.sub(r'("pedestal_depth"\s*=\s*)[0-9.]+', f'\\g<1>{depth}', original_content)
    write_default_file(default_txt_path, new_content)
    # time.sleep(1.0)
    rebuild_model(swModel)
    step_filename = f"{depth}mm_pedestal.STEP"
    step_filepath = os.path.join(experiment_dir, step_filename)
    logging.info("Saving step file: %s", step_filepath)
    success, err = save_as_step(swModel, step_filepath)
    if success:
        logging.info("Successfully saved %s", step_filepath)
    else:
        logging.error("Failed to save %s: %s", step_filepath, err)


def create_step_files(depths: List[float], project_dir: str) -> None:
    base_parts_dir = os.path.join(project_dir, 'base_parts')
    experiment_dir = os.path.join(project_dir, 'pedestal_experiment')

    default_txt_path = os.path.join(base_parts_dir, 'default.txt')
    dimensions_txt_path = os.path.join(experiment_dir, 'dimensions.txt')
    assembly_path = os.path.join(base_parts_dir, 'all_components.SLDASM')

    if not os.path.exists(experiment_dir):
        os.makedirs(experiment_dir)

    swApp = connect_solidworks()
    if not swApp:
        return

    logging.info("Reading original default.txt...")
    original_content = read_default_file(default_txt_path)
    shutil.copy(default_txt_path, dimensions_txt_path)

    swModel = open_and_activate_assembly(swApp, assembly_path)
    if not swModel:
        # restore original default before exiting
        write_default_file(default_txt_path, original_content)
        return

    for depth in depths:
        process_depth(depth, default_txt_path, original_content, experiment_dir, swModel)

    logging.info("Restoring original default.txt...")
    write_default_file(default_txt_path, original_content)
    logging.info("Experiment completed successfully.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create pedestal STEP files at multiple depths')
    parser.add_argument('--depths', '-d', nargs='+', type=float, default=[0, 2, 4, 6, 8, 10],
                        help='List of pedestal depths in mm')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_directory = os.path.abspath(os.path.dirname(__file__))
    create_step_files(args.depths, project_directory)


if __name__ == "__main__":
    main()
