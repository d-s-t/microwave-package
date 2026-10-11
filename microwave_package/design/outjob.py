"""
The Altium Output Job and project that turn a generated board into
fabrication files.

Altium has no scriptable call that writes a Gerber, ODB++ or IPC-2581
set without showing a dialog; what it does run unattended is an Output
Job (`.OutJob`) in the board's project. So the manufacturing export
writes both files as text, opens the project, and tells Altium to run the
job -- see `base_parts/altium/ExportManufacturing.pas`.

The text follows the Output Jobs Altium ships (under
`Documents\\Altium\\AD26\\OutputJobs`): one `[OutputGroup1]` listing the
outputs, one `Configuration<n>_Item1` of `|`-separated settings each, and a
`[PublishSettings]` naming the folder the "generated files" medium writes
to. Settings left out take Altium's defaults.
"""
from __future__ import annotations

from typing import List, Tuple

# Layer ids (Altium's TV7_Layer values) the Gerber output plots.
LAYER_IDS = {
    "top": 16777217, "bottom": 16842751,
    "top_overlay": 16973830, "bottom_overlay": 16973831,
    "top_paste": 16973832, "bottom_paste": 16973833,
    "top_solder": 16973834, "bottom_solder": 16973835,
    # Mechanical 2, where GenerateFromParams.pas draws the board outline: a
    # Gerber set has no board shape of its own, so the outline is a layer.
    "outline": 16908290,
}


def _plot_set() -> str:
    return "SerializeLayerHash.Version~2,ClassName~TLayerToBoolean," + ",".join(
        f"{layer_id}~1" for layer_id in LAYER_IDS.values())


def outputs() -> List[Tuple[str, str, str, str]]:
    """(type, name, category, configuration) of each output, in order."""
    return [
        ("Gerber", "Gerber", "Fabrication",
         "Record=GerberView|GerberUnit=Metric|NumberOfDecimals=6|"
         "LeadingAndTrailingZeroesMode=SuppressLeadingZeroes|EmbeddedApertures=True|"
         "GenerateDRCRulesFile=False|GenerateReliefShapes=True|"
         f"Plot.Set={_plot_set()}"),
        ("NC Drill", "NC Drill", "Fabrication",
         "Record=DrillView|Units=Metric|NumberOfUnits=3|NumberOfDecimals=3|"
         "ZeroesMode=SuppressTrailingZeroes|GenerateSeparatePlatedNonPlatedFiles=True|"
         "GenerateBoardEdgeRout=False|GenerateEIADrillFile=False|OriginPosition=Relative"),
        ("ODB", "ODB++", "Fabrication",
         "Record=ODBView|ODBProfileLayer=-1000|PlotTopLayerPlot=True|PlotBottomLayerPlot=True|"
         "PlotTopOverlayPlot=True|PlotBottomOverlayPlot=True|PlotTopSolderPlot=True|"
         "PlotBottomSolderPlot=True|PlotTopPastePlot=True|PlotBottomPastePlot=True"),
        ("IPC2581", "IPC-2581", "Fabrication",
         "Record=IPC2581View|IPC2581Version=B|MeasurementSystem=Metric|"
         "FloatingPointPrecision=6|IsWiseSupport=False"),
        ("ExportSTEP", "STEP", "Export",
         "Record=ExportSTEPView|ExportComponentOptions=0|ExportModelsOption=2|ExportHolesOption=0|"
         "CanSelectPrimitives=False|IncludeMechanicalPadHoles=False|IncludeElectricalPadHoles=False|"
         "IncludeFreePadHoles=False|ExportFoldedBoard=True|ExportFoldedBoardRate=100|"
         "ComponentSuffixType=0|ComponentSuffix= |ExportCopperOption=0|ExportCopperLayer=1|"
         "ExportPadAndViaBarrelsOnly=False|IgnoreBoardCopperLayerColors=False|ExportAsSinglePart=False"),
    ]


def text(output_folder: str) -> str:
    """The OutJob, every output writing into `output_folder`."""
    lines = [
        "[OutputJobFile]", "Version=1.0", "Caption=", "Description=", "",
        "[OutputGroup1]", "Name=", "Description=",
        "TargetOutputMedium=Folder Structure", "VariantName=[No Variations]", "VariantScope=0",
        "CurrentConfigurationName=",
        "OutputMedium1=Print Job", "OutputMedium1_Type=Printer", "OutputMedium1_Printer=",
        "OutputMedium2=PDF", "OutputMedium2_Type=Publish",
        "OutputMedium3=Folder Structure", "OutputMedium3_Type=GeneratedFiles",
    ]
    for number, (kind, name, category, configuration) in enumerate(outputs(), start=1):
        lines += [
            f"OutputType{number}={kind}", f"OutputName{number}={name}",
            f"OutputCategory{number}={category}", f"OutputDocumentPath{number}=",
            f"OutputVariantName{number}=", f"OutputEnabled{number}=1",
            f"OutputEnabled{number}_OutputMedium1=0", f"OutputEnabled{number}_OutputMedium2=0",
            f"OutputEnabled{number}_OutputMedium3={number}",
            f"OutputDefault{number}=0",
            f"Configuration{number}_Name1=OutputConfigurationParameter1",
            f"Configuration{number}_Item1={configuration}",
        ]
    folder = output_folder.rstrip("\\/") + "\\"
    lines += [
        "", "[PublishSettings]",
        f"OutputFilePath3={folder}", "ReleaseManaged3=0", "OutputBasePath3=",
        "OutputPathMedia3=", "OutputPathMediaValue3=", "OutputPathOutputer3=[Output Type]",
        "OutputPathOutputerPrefix3=", "OutputPathOutputerValue3=", "OutputFileName3=",
        "OutputFileNameMulti3=", "UseOutputNameForMulti3=1", "OutputFileNameSpecial3=",
        "OpenOutput3=0", "",
        "[GeneratedFilesSettings]", f"RelativeOutputPath3={folder}", "OpenOutputs3=0", "",
    ]
    return "\r\n".join(lines)


def project_text(pcb_name: str, outjob_name: str) -> str:
    """A PCB project holding the board and its output job."""
    from ..workflow import manufacturing

    base = manufacturing.project_text(pcb_name).rstrip("\r\n")
    return base + "\r\n" + "\r\n".join([
        "[Document2]", f"DocumentPath={outjob_name}", "AnnotationEnabled=1",
        "AnnotateStartValue=1", "AnnotationIndexControlEnabled=0", "AnnotateSuffix=",
        "AnnotateScope=All", "AnnotateOrder=-1", "DoLibraryUpdate=1", "DoDatabaseUpdate=1",
        "ClassGenCCAutoEnabled=1", "ClassGenCCAutoRoomEnabled=1", "ClassGenNCAutoScope=None",
        "DItemRevisionGUID=", "GenerateClassCluster=0", "DocumentUniqueId=", ""])
