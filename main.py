import argparse
import asyncio
import datetime
import logging
import os
import sys
from datetime import datetime

from odk_tools.tracking import Tracker
from kmd_nexus_client import NexusClientManager 
from kmd_nexus_client.tree_helpers import filter_by_path
from automation_server_client import AutomationServer, Workqueue, WorkItemError, Credential
from excel_loader import load_indsatser_list

nexus: NexusClientManager
tracker: Tracker
procesnavn = "Automatisk filtrering af Medcoms for Sambovagten"


def fetch_activities_from_nexus() -> dict:
    """
    Fetch activities from Nexus API and return them as a dictionary.
    
    Returns:
        Dictionary of activities keyed by activity ID
    """
    logger = logging.getLogger(__name__)
    
    præferencer = nexus.nexus_client.get("preferences").json()
    #look through ACTIVITY_LIST and find the one thats called "MedCom - Plejeforløbsplaner + Udskrivningsrapporter"
    aktivitetsliste = next((item for item in præferencer.get("ACTIVITY_LIST", []) if item.get("name") == "MedCom - Plejeforløbsplaner + Udskrivningsrapporter"), None)
    if not aktivitetsliste:
        logger.error("Could not find 'MedCom - Plejeforløbsplaner + Udskrivningsrapporter' in ACTIVITY_LIST")
        return {}
    aktivitetsliste = nexus.nexus_client.get(aktivitetsliste["_links"]["self"]["href"]).json()
    
    # Get the base content URL
    base_content_url = aktivitetsliste["_links"]["content"]["href"]
    content_url = base_content_url + "&pageSize=50&assignmentOrganizationAssignee=ALL_ORGANIZATIONS&assignmentProfessionalAssignee=NO_PROFESSIONAL_CRITERIA"
    
    logger.info(f"Fetching activities from Nexus...")
    
    activities_dict = {}  # Initialize the dictionary to store activities
    try:
        response = nexus.nexus_client.get(content_url)
        activities_data = response.json()
        pages = activities_data["pages"]
        logger.info(f"Found {len(pages)} pages to process")
        
        for i, page in enumerate(pages):
            logger.info(f"Processing page {i+1}/{len(pages)}")
            
            temp_activity = nexus.nexus_client.get(page["_links"]["content"]["href"]).json()
            
            # Handle the case where temp_activity is a list of activities
            if isinstance(temp_activity, list):
                for activity in temp_activity:
                    if isinstance(activity, dict) and "id" in activity:
                        activities_dict[activity["id"]] = activity
                
        logger.info(f"Successfully loaded {len(activities_dict)} activities into dictionary")
        return activities_dict
        
    except Exception as e:
        logger.error(f"Failed to fetch activities from Nexus: {e}")
        raise


async def populate_queue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from populate workqueue!")

    activities_dict = fetch_activities_from_nexus()
    
    if not activities_dict:
        logger.warning("No activities found, nothing to process")
        return
    
    for aktivitet in activities_dict.values():
        ignore_activity = False
        borger = nexus.borgere.hent_borger(aktivitet["patients"][0]["patientIdentifier"]["identifier"])
        pathway = nexus.borgere.hent_visning(borger)
        borgers_indsatsreferencer = nexus.borgere.hent_referencer(pathway)
        filtrerede_indsats_referencer = filter_by_path(
            borgers_indsatsreferencer,
            path_pattern="/Sundhedsfagligt grundforløb/*/Indsatser/*",
            active_pathways_only=False,
        )

        # Hvis borger ikke har nogen indsatsreferencer, så tilføj denne aktivitet
        if filtrerede_indsats_referencer is None:
            workqueue.add_item(
                data={
                    "Medkom-Id": aktivitet["id"],
                    "Cpr": borger["patientIdentifier"]["identifier"]
                },
                reference=f"{aktivitet['id']}",
            )
            continue

        # Check if any of the filtered indsats references match the Excel data
        for indsats_ref in filtrerede_indsats_referencer:
            if ignore_activity:
                break
            # Get the name of the indsats reference
            indsats_name = indsats_ref.get("name", "")
            
            # Find all matching interventions from Excel list
            indsats_name_lower = indsats_name.lower()
            matching_interventions = [
                excel_indsats for excel_indsats in indsatser_list 
                if excel_indsats.lower() in indsats_name_lower
            ]

            if matching_interventions:
                indsats = nexus.indsatser.hent_indsats(indsats_ref)
                if indsats["workflowState"]["name"] in ["Bestilt", "Ændret", "Bevilliget", "Anvist", "Fremtidigt ændret"]:
                    ignore_activity = True
                    logger.info(f"Ignoring activity - found matching active intervention: {indsats_name} (matches: {matching_interventions})")
                    break

        # If we are ignoring this activity, skip it
        if ignore_activity:
            continue

        # If we reach this point, the activity is not ignored and data is added to the workqueue
        workqueue.add_item(
            data={
                "Medkom-Id": aktivitet["id"],
                "Cpr": borger["patientIdentifier"]["identifier"]
            },
            reference=f"{aktivitet['id']}",
        )

        print("hej")

    print("stop")
async def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    logger.info("Hello from process workqueue!")

    for item in workqueue:
        with item:
            data = item.data  # Item data deserialized from json as dict
 
            try:
                # Find den rette besked
                borger = nexus.borgere.hent_borger(data["Cpr"])
                indbakke = nexus.medcom.hent_alle_beskeder(borger)
                beskedreference = next((b for b in indbakke if b["id"] == data["Medkom-Id"]), None)
                if beskedreference is None:
                    raise ValueError(f"Besked ikke fundet med id: {data['Medkom-Id']}")
                besked_der_skal_arkiveres = nexus.medcom.hent_besked(beskedreference)

                # Opret opgave:
                opgave = nexus.opgaver.opret_opgave(
                    objekt=besked_der_skal_arkiveres,
                    opgave_type="Leverandørvalg",
                    titel="Leverandørvalg",
                    ansvarlig_organisation="MedCom SAMBOvagt",
                    start_dato=datetime.today(),
                    forfald_dato=datetime.today()
                )
                if opgave is None:
                    raise ValueError(f"Kunne ikke oprette opgave for besked: {data['Medkom-Id']}")
                
                # Arkivér besked:
                nexus.medcom.arkiver_besked(besked_der_skal_arkiveres)
                tracker.track_task(procesnavn)

            except WorkItemError as e:
                # A WorkItemError represents a soft error that indicates the item should be passed to manual processing or a business logic fault
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))


if __name__ == "__main__":
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Automatisk filtrering af MedComs for Sambovagten")
    parser.add_argument(
        "--excel-file",
        default="./Indsatser.xlsx",
        help="Path to the Excel file containing filtering data (default: ./Indsatser.xlsx)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Populate the queue with test data and exit",
    )
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Validate Excel file exists
    if not os.path.isfile(args.excel_file):
        raise FileNotFoundError(f"Excel file not found: {args.excel_file}")

    # Load indsatser list once on startup
    indsatser_list = load_indsatser_list(args.excel_file)

    ats = AutomationServer.from_environment()

    workqueue = ats.workqueue()

    

    # Initialize external systems for automation here..
    nexus_credential = Credential.get_credential("KMD Nexus - produktion")
    tracking_credential = Credential.get_credential("Odense SQL Server")


    nexus = NexusClientManager(
        client_id=nexus_credential.username,
        client_secret=nexus_credential.password,
        instance=nexus_credential.data["instance"],
    )    

    tracker = Tracker(
        username=tracking_credential.username, 
        password=tracking_credential.password
    )

    # Queue management
    if args.queue:
        workqueue.clear_workqueue("new")
        asyncio.run(populate_queue(workqueue))
        exit(0)

    # Process workqueue
    asyncio.run(process_workqueue(workqueue))