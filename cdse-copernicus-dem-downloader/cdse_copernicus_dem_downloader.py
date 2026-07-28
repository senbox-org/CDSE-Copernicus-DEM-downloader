#!/usr/bin/env python
# Copyright (c) 2026
#
# Authors:
# Sen2Cor_dev_team (Telespazio)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A Tool for accessing Copernicus DEM geocells in different format (DTED, DGED) at a given resolution (30 m or 90 m) (1.0 or 3.0 arc-second)"""

# Built-in Python modules
import argparse
import datetime
import glob
import json
import logging
import os
import pathlib
import shutil
import sys
from zipfile import ZipFile

# Python modules from additional packages
import antimeridian as am
import pandas as pd
import regex as re
import requests
import shapely.wkt as wkt
from colorlog import ColoredFormatter
from credentials.credentials import Credentials
from lxml import objectify
from pykml import parser
from requests.exceptions import HTTPError
from shapely.geometry import Polygon

# Set up the logging
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(log_color)s %(asctime)s | %(log_color)s %(levelname)s %(message)s"
DATE_FORMAT = "%m/%d/%Y %I:%M:%S %p"

logging.root.setLevel(LOG_LEVEL)
formatter = ColoredFormatter(LOG_FORMAT, DATE_FORMAT)
stream = logging.StreamHandler()
stream.setLevel(LOG_LEVEL)
stream.setFormatter(formatter)
log = logging.getLogger("pythonConfig")
log.setLevel(LOG_LEVEL)
log.addHandler(stream)


class DemDownloaderException(Exception):
    pass


KML_DOWNLOAD_URL = (
    "https://sentiwiki.copernicus.eu/__attachments/"
    "a_69a662aa1cb30487acc66009f09b1dcfa9e4e32f92af1c3b2dd1fc8a3c011738/"
    "S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.zip"
)


class DemDownloader:
    def __init__(self):
        self.lon_max = 0
        self.lon_min = 0
        self.lat_min = 0
        self.lat_max = 0
        self.kml_file = (
            "S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_21000101T000000_B00.kml"
        )
        self.cfg_file = "configuration.xml"
        self.input_file = "input_tiles.txt"
        self.dem_resolution = "90"
        self.dem_collection = "CCM"
        self.dem_format = "DGED"
        self.dem_delivery_id = "2024_1"
        self.dem_search_url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products?$filter="  # the main URL for the DEM retrieval
        self.home_directory = pathlib.Path(__file__).parent.resolve()
        self.dem_directory = os.path.join(self.home_directory, "output_dir")
        self.aux_directory = os.path.join(self.home_directory, "auxiliary")  # aux_directory
        config_directory = os.path.join(self.home_directory, "configuration")  # config_directory
        self.input_tile_file = os.path.join(config_directory, self.input_file)
        self.configuration_file = os.path.join(config_directory, self.cfg_file)
        self.tiles_id_list = []
        self.tiles_id_antimeridian = []
        self.polygon_id_antimeridian = []
        self.antimeridian_status = False
        self.missing_tiles_list = []
        self.polygon = []
        self.shape_polygon = []
        self.tile_coordinates = []
        self.processed_dem_id_file = []
        self.dict_filename_dem_id_file = "dict_filename_dem_id.json"
        self.dict_filename_dem_id = {}
        self.region = False

        self.version_tool = 1.1
        self.version_date = "28-July-2026"
        self.tool_info = (
            "DEM Downloader "
            + "Version: "
            + str(self.version_tool)
            + " Release Date: "
            + self.version_date
        )

        self.username = ""
        self.password = ""

    @staticmethod
    def get_access_token(username: str, password: str) -> tuple[str, str]:

        data = {
            "client_id": "cdse-public",
            "username": username,
            "password": password,
            "grant_type": "password",
        }
        try:
            r = requests.post(
                "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
                data=data,
                timeout=(30.0, 20.0),
            )
            r.raise_for_status()
        except HTTPError as http_error:
            raise DemDownloaderException(
                f"Access token creation failed. Response from the server was: {r.json()}"
            ) from http_error

        return r.json()["access_token"], r.json()["refresh_token"]

    @staticmethod
    def refresh_access_token(refresh_token: str) -> tuple[str, str]:
        data = {
            "client_id": "cdse-public",
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        try:
            r = requests.post(
                "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
                data=data,
                timeout=(30.0, 20.0),
            )
            r.raise_for_status()
        except HTTPError as http_error:
            raise DemDownloaderException(
                f"Refresh access token failed. Response from the server was: {r.json()}"
            ) from http_error

        return r.json()["access_token"], r.json()["refresh_token"]

    def open_compressed_file(self, dem_file, dem_id):

        with ZipFile(dem_file) as zip_object:
            for member in zip_object.namelist():
                if re.match(r".*DEM.tif$", member) or re.match(r".*DEM.dt[12]$", member):
                    filename = os.path.basename(member)
                    # skip directories
                    if not filename:
                        continue
                    # copy file (taken from zipfile's extract)
                    source = zip_object.open(member)
                    target = open(os.path.join(self.dem_directory, filename), "wb")
                    with source, target:
                        shutil.copyfileobj(source, target)
                        log.info("DEM %s extracted and stored", filename)
                        self.processed_dem_id_file.append(dem_id)
                        self.dict_filename_dem_id[filename] = dem_id
        try:
            os.remove(dem_file)
        except:
            pass

        return

    def downloading_dem(self, dem, access_token):

        dwn_url = f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({dem})/$value"
        headers = {"Authorization": f"Bearer {access_token}"}

        session = requests.Session()
        session.headers.update(headers)
        response = session.get(dwn_url, headers=headers, stream=True)

        if response.status_code == 403:
            try:
                error_body = response.json()
            except ValueError:
                error_body = {}
            if error_body.get("code") == "DAT-ZIP-608":
                raise DemDownloaderException(
                    "CDSE refused the download (403 DAT-ZIP-608: Access forbidden).\n\n"
                    "To download Copernicus Contributing Missions (CCM) data, users must "
                    "update their Copernicus Data Space Ecosystem (CDSE) user profile by:\n"
                    '- enabling the option "I am also interested in accessing Copernicus '
                    'Contributing Missions data";\n'
                    '- ticking "Accept ESA-User license for the use of CCM data and CCM '
                    'Data Access Restrictions".'
                )

            raise DemDownloaderException(
                f"CDSE refused the download of {dem} (HTTP 403): {error_body or response.text}"
            )

        if not os.path.exists(self.dem_directory):
            log.warning("%s does not exist and it will be created", self.dem_directory)
            os.makedirs(self.dem_directory)

        file_string_name = f"{self.dem_directory}/{dem}.zip"
        with open(f"{self.dem_directory}/{dem}.zip", "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file.write(chunk)

        file.close()
        log.info("File %s downloaded", dem)

        try:
            self.open_compressed_file(file_string_name, dem)
        except:
            log.warning("Error in extracting the DEM file from the zip file, skipping")

        return

    @staticmethod
    def clean_multipolygon(polygon_raw):

        polygon_temp = polygon_raw.replace("MULTIPOLYGON(((", "")
        polygon_raw = polygon_temp.replace(")))", "")
        return polygon_raw

    def is_antimeridian(self, polygon_input):
        polygon_input = polygon_input.replace("MULTIPOLYGON(((", "POLYGON((")
        polygon_input = polygon_input.replace(")))", "))")
        shapelyobject = wkt.loads(polygon_input)
        self.shape_polygon = Polygon(shapelyobject)
        # minx, miny, maxx, maxy
        bounds = Polygon(shapelyobject).bounds
        # print(bounds)
        difference = bounds[2] - bounds[0]
        # print(difference)
        if abs(difference) > 180:
            log.info("Tile crosses antimeridian")
            return True

        return False

    def ensure_kml_file(self):

        kml_path = os.path.join(self.aux_directory, self.kml_file)
        if os.path.isfile(kml_path):
            return kml_path

        log.warning("Sentinel-2 tiling system KML file not found: %s", kml_path)
        log.info("Attempting automatic download from %s", KML_DOWNLOAD_URL)

        try:
            response = requests.get(KML_DOWNLOAD_URL, timeout=(30.0, 60.0))
            response.raise_for_status()

            os.makedirs(self.aux_directory, exist_ok=True)
            zip_path = os.path.join(self.aux_directory, "s2_tiling_kml.zip")
            with open(zip_path, "wb") as zip_file:
                zip_file.write(response.content)

            with ZipFile(zip_path) as archive:
                kml_members = [name for name in archive.namelist() if name.lower().endswith(".kml")]
                if not kml_members:
                    raise DemDownloaderException("downloaded archive contains no KML file")
                with archive.open(kml_members[0]) as source, open(kml_path, "wb") as target:
                    shutil.copyfileobj(source, target)

            os.remove(zip_path)
            log.info("Sentinel-2 tiling system KML file downloaded and saved to %s", kml_path)
            return kml_path

        except Exception as download_error:
            raise DemDownloaderException(
                f"Sentinel-2 tiling system KML file is missing ({kml_path}) and automatic "
                f"download failed ({download_error}). Please download it manually from "
                f"{KML_DOWNLOAD_URL}, unzip it, and place the .kml file in "
                f"{self.aux_directory}, then retry."
            ) from download_error

    def retrieve_multipolygon(self, sentinel_2_tile, tile_id):

        with open(sentinel_2_tile, "r", encoding="utf-8") as f:
            root = parser.parse(f).getroot()

        for place in root.Document.Folder.Placemark:
            tile = place.name
            if tile == tile_id:
                log.info("Tile_ID: %s", tile_id)
                temp_1 = place.description.text.split("LL_WKT")[1]
                temp_2 = temp_1.split('<font COLOR="#008000">')[1]
                tile_geometry = temp_2.split("</font>")[0]
                log.info("Tile Found")
                break
        f.close()

        try:
            self.antimeridian_status = self.is_antimeridian(tile_geometry)
            self.polygon = self.clean_multipolygon(tile_geometry)
            return self.polygon
        except:
            log.warning("Multipolygon: Tile not found, check if the spelling is correct")
            return False

    def create_url(self):

        dataset = f"COP-DEM_GLO-{self.dem_resolution}-{self.dem_format}/{self.dem_delivery_id}"  #e.g. "COP-DEM_GLO-30-DGED"
        collection_req = f"Collection/Name eq '{self.dem_collection}'"
        dataset_req = f"Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'dataset' and att/OData.CSC.StringAttribute/Value eq '{dataset}')"
        polygon_req = f"OData.CSC.Intersects(area=geography'SRID=4326;POLYGON(({self.polygon}))')"
        max_n_items = "$top=100"  # default 20
        url = f"{self.dem_search_url}{collection_req} and {dataset_req} and {polygon_req}&{max_n_items}"
        log.info("URL for Request created")

        return url

    @staticmethod
    def retrieve_dem_list(url_path):

        json_dict = requests.get(url_path, timeout=(30.0, 20.0)).json()
        try:
            if json_dict["value"]:
                log.info("DEM_LIST from url retrieved")
                df_dem_list = pd.DataFrame.from_dict(json_dict["value"])
                return df_dem_list["Id"].tolist()
            else:
                return []
        except:
            log.error(json_dict)
            return 1

    def reading_arguments(self, arguments):

        self.dem_resolution = arguments.resolution if arguments.resolution is not None else self.dem_resolution
        self.dem_format = arguments.model if arguments.model is not None else self.dem_format
        self.dem_directory = arguments.output if arguments.output is not None else self.dem_directory
        if arguments.tile:
            self.tiles_id_list = self.if_safe([arguments.tile])
        elif arguments.input:
            self.input_tile_file = arguments.input
            self.tiles_id_list = self.read_input_tile_list(self.input_tile_file)
        else:
            log.warning("Default Tool Input_Tiles file will be used: input_tiles.txt")

        return

    def reading_xml_parameters(self, xml_path):

        try:
            with open(xml_path, encoding="UTF-8") as f:
                xml = f.read().encode()
                root = objectify.fromstring(xml)

                if (
                    root.DEM_Option.Resolution == "DEFAULT"
                    or root.DEM_Option.Collection == "DEFAULT"
                    or root.DEM_Option.Elevation_Model == "DEFAULT"
                    or root.DEM_Option.Delivery_Id == "DEFAULT"
                ):
                    log.warning(
                        "At least one between dem_collection, dem_format, dem_resolution, dem_delivery_id is NONE"
                    )
                    log.warning("Default COP-DEM_GLO-30-DGED/2024_1 will be used")
                else:
                    self.dem_resolution = str(root.DEM_Option.Resolution)
                    self.dem_collection = str(root.DEM_Option.Collection)
                    self.dem_delivery_id = str(root.DEM_Option.Delivery_Id)
                    self.dem_format = str(root.DEM_Option.Elevation_Model)
                if root.DEM_Option.Tiles_Input_File != "DEFAULT":
                    self.input_tile_file = str(root.DEM_Option.Tiles_Input_File)
                else:
                    log.warning("Default Tool Input_Tiles file will be used: input_tiles.txt")
                if root.DEM_Option.DEM_Output_Directory != "DEFAULT":
                    self.dem_directory = str(root.DEM_Option.DEM_Output_Directory)
                else:
                    log.warning("Default Tool Directory will be used: Output_dir")

            return True

        except:
            log.warning("Problems with reading the configuration.xml Parameters")
            log.warning("Default Default COP-DEM DGED 90  and Output_dir will be used")
            f.close()
            return False

    @staticmethod
    def if_safe(tile_list):

        tile_list = list(
            map(lambda tile: tile.split("_")[5][1:] if "SAFE" in tile else tile, tile_list)
        )

        return tile_list

    @staticmethod
    def if_comment(tile_list):

        tile_list = list(map(lambda tile: tile[0:5] if "#" in tile else tile, tile_list))

        return tile_list

    def read_input_tile_list(self, input_file_tile):
        with open(input_file_tile, "r", encoding="UTF-8") as file:
            tiles_id = file.read().splitlines()
            tiles_id = self.if_safe(tiles_id)
            tiles_id = self.if_comment(tiles_id)
        file.close()

        return tiles_id


def main(argv: list[str]) -> int:

    dem_downloader = DemDownloader()

    arg_parser = argparse.ArgumentParser(
        description=str(dem_downloader.tool_info) + ".", add_help=True
    )
    arg_parser.add_argument(
        "--config",
        help=r"set the path to the Config file. If blank, read the parameters from the  configuration/configuration.xml",
        nargs="?",
        const="Default",
    )
    arg_parser.add_argument(
        "-r",
        "--resolution",
        type=str,
        choices=["30", "90"],
        help="Set the (r)esolution of the DEM: 30 or 90 (m)",
    )
    arg_parser.add_argument(
        "-m",
        "--model",
        choices=["DTED", "DGED"],
        help="Set the (m)odel of the DEM: DTED or DGED",
    )
    arg_parser.add_argument(
        "-d",
        "--delivery_id",
        choices=[
            "2019_1",
            "2019_2",
            "2020_1",
            "2020_2",
            "2021_1",
            "2021_2",
            "2022_1",
            "2023_1",
            "2024_1",
        ],
        help="Set the (d)elivery_id of the DEM: 2019_1, 2019_2, 2020_1, 2020_2, "
             "2021_1, 2021_2, 2022_1, 2023_1, 2024_1",
    )
    arg_parser.add_argument(
        "-o",
        "--output",
        help=r"Set the (o)utput directory for storing the DEM. If blank, store into the Tool's Output_Dir",
    )
    arg_parser.add_argument(
        "-i",
        "--input",
        help=r"Set the path for the (i)nput file containing the tiles list. If blank, read from the Tool's configuration/input_tiles.txt",
    )
    arg_parser.add_argument(
        "-t",
        "--tile",
        help="Specify a single required MGRS (t)ile. e.g. 32UMA or Product (SAFE)",
    )
    arg_parser.add_argument(
        "--reset",
        help="Reset credentials",
        nargs="?",
        const="RESET",
    )

    args = arg_parser.parse_args()

    log.info("<----------------------------------------------------------------------->")
    log.info("CDSE Copernicus DEM Downloader: Copernicus DEM retrieval for Sen2Cor")
    log.info("Version: %s Date: %s", dem_downloader.version_tool, dem_downloader.version_date)
    log.info("<----------------------------------------------------------------------->")

    if args.reset:
        credentials = (
            pathlib.Path(os.path.realpath(__file__)).parent / "credentials" / "credentials.yaml"
        )
        log.warning("Do you confirm you want to reset your credentials? (y / n)")
        answer = input()
        if answer in ("y", "Y"):
            open(credentials, "w", encoding="UTF-8").close()
            log.info("Credentials file erased: %s", credentials)
            Credentials()
        else:
            log.info("Credentials file kept unchanged: %s", credentials)
        log.info("Credentials --reset process will stop")
        return 0

    try:
        sentinel2_tiles_list = dem_downloader.ensure_kml_file()
    except DemDownloaderException as kml_error:
        log.error(str(kml_error))
        return 1

    if args.config:
        if args.config != "Default":
            dem_downloader.configuration_file = args.config
            log.info("Reading Request from Config File: %s", dem_downloader.configuration_file)
        else:
            log.info(
                "Reading Request from Default Config File: %s", dem_downloader.configuration_file
            )

        dem_downloader.reading_xml_parameters(dem_downloader.configuration_file)
        # dem_downloader.input_tile_file = os.path.join(dem_downloader.CFG_Directory, dem_downloader.input_file)
        dem_downloader.tiles_id_list = dem_downloader.read_input_tile_list(
            dem_downloader.input_tile_file
        )

    else:
        log.info("Reading Request from Command Line")
        dem_downloader.reading_arguments(args)

    log.info("Processing %s Tile(s)", len(dem_downloader.tiles_id_list))

    # List the Copernicus DEM files present in local dem_downloader.dem_directory (*.tif, *.dt1, *.dt2):
    dem_types = ("Copernicus*DEM.tif", "Copernicus*DEM.dt1", "Copernicus*DEM.dt2")
    dem_files_list = []
    for dem_type in dem_types:
        dem_files_list.extend(
            [
                os.path.basename(f)
                for f in glob.glob(os.path.join(dem_downloader.dem_directory, dem_type))
            ]
        )

    # Read JSON file back into a dictionary
    dict_filename_dem_id_file = os.path.join(
        dem_downloader.aux_directory, dem_downloader.dict_filename_dem_id_file
    )

    # Use dictionary to construct Dem ids from Copernicus DEM filenames
    # Dem ids are necessary to identify Dem tiles available on CDSE and prevent duplicate downloads
    dem_id_stored = []
    if os.path.isfile(dict_filename_dem_id_file):
        with open(dict_filename_dem_id_file, "r", encoding="UTF-8") as json_file:
            try:
                dem_downloader.dict_filename_dem_id = json.load(json_file)

                for key in dem_files_list:
                    try:
                        dem_id_stored.append(dem_downloader.dict_filename_dem_id[key])
                    except:
                        log.warning("No entry found for: %s", key)

            except:
                log.warning("Error reading dictionary file: %s", dict_filename_dem_id_file)

    log.info("Getting access token")
    try:
        # request access
        auth = Credentials()
        dem_downloader.username = auth.id
        dem_downloader.password = auth.password
        access_token, refresh_token = dem_downloader.get_access_token(
            dem_downloader.username, dem_downloader.password
        )
    except:
        log.error("Error in getting access token")
        log.warning("Please check and reset your Credentials with --reset option")
        log.warning("Process will stop")
        return 1

    start_time = datetime.datetime.now()
    # test exit > 60 minutes
    # start_time = datetime.datetime.now() - datetime.timedelta(seconds=3600)
    # test exit > 10 minutes
    # ystart_time = datetime.datetime.now() - datetime.timedelta(seconds=600)
    token_time = start_time
    log.info("Start time: %s", start_time)

    for tile_id in dem_downloader.tiles_id_list:

        log.info("<----------------------------------------------------------------------->")
        dem_downloader.retrieve_multipolygon(sentinel2_tiles_list, tile_id)

        required_polygons = []
        if dem_downloader.antimeridian_status:
            log.warning("%s is antimeridian, special processing will occur", tile_id)
            # fixing polygon here... create two polygons and for each of those polygons proceed with the similar process as above

            polygon_output = am.fix_polygon(polygon=dem_downloader.shape_polygon)
            antimeridian_polygons = list(polygon_output.geoms)
            dem_downloader.antimeridian_status = False
            for anti_polygon in antimeridian_polygons:
                simple_polygon = re.sub(r"^.*?POLYGON \(\(", "", str(anti_polygon))
                simple_polygon = simple_polygon.replace(")", "")
                required_polygons.append(simple_polygon)
        else:
            required_polygons.append(dem_downloader.polygon)

        for polygon in required_polygons:
            dem_downloader.polygon = polygon
            url = dem_downloader.create_url()
            dem_list = dem_downloader.retrieve_dem_list(url)

            if dem_list == 1:
                log.error("Error in the request for %s. %s will be skipped", tile_id, tile_id)
                continue

            if dem_list:
                log.info("There are %s DEM files expected", len(dem_list))

                for counter, dem_id in enumerate(dem_list):
                    log.info("Processing file n %s", counter + 1)

                    if (datetime.datetime.now() - token_time) < datetime.timedelta(seconds=600):
                        log.info("Token is still valid, within its 10 minutes period")
                    elif (datetime.datetime.now() - start_time) < datetime.timedelta(seconds=3600):
                        try:
                            access_token, refresh_token = dem_downloader.refresh_access_token(
                                refresh_token
                            )
                        except:
                            log.error("Error when refreshing access token")
                            log.warning("Process will stop")
                            return 1

                        token_time = datetime.datetime.now()
                        log.warning("Token was refreshed for 10 minutes")
                    else:
                        # Save dictionary name:id to a JSON file
                        dict_filename_dem_id_file = os.path.join(
                            dem_downloader.aux_directory, dem_downloader.dict_filename_dem_id_file
                        )
                        with open(dict_filename_dem_id_file, "w", encoding="UTF-8") as json_file:
                            json.dump(dem_downloader.dict_filename_dem_id, json_file)
                        log.info("Dictionary name:id saved to JSON file")
                        log.warning("60 minutes token duration is expired")
                        log.warning("Process will stop")

                        return 0

                    if dem_id in dem_downloader.processed_dem_id_file:
                        log.warning("File %s was already processed from an adjacent tile", dem_id)
                    elif dem_id_stored:
                        if dem_id in dem_id_stored:
                            log.warning("File %s is already present on local storage", dem_id)
                        else:
                            try:
                                dem_downloader.downloading_dem(dem_id, access_token)
                            except DemDownloaderException as download_error:
                                log.error(str(download_error))
                                log.warning("Process will stop")
                                return 1
                            except:
                                log.warning(
                                    "Your access token looks invalid, please check and reset your Credentials"
                                )
                                log.warning("Process will stop")
                                return 1

                    else:
                        try:
                            dem_downloader.downloading_dem(dem_id, access_token)
                        except DemDownloaderException as download_error:
                            log.error(str(download_error))
                            log.warning("Process will stop")
                            return 1
                        except:
                            log.warning(
                                "Your access token looks invalid, please check and reset your Credentials"
                            )
                            log.warning("Process will stop")
                            return 1

            else:
                log.warning("NO DEM available for Tile %s", tile_id)

            # Save dictionary name:id to a JSON file (after each tile processed)
            dict_filename_dem_id_file = os.path.join(
                dem_downloader.aux_directory, dem_downloader.dict_filename_dem_id_file
            )
            with open(dict_filename_dem_id_file, "w", encoding="UTF-8") as json_file:
                json.dump(dem_downloader.dict_filename_dem_id, json_file)
            log.info("Dictionary name:id saved to JSON file")

    log.info("Process is finished")
    return 0


if __name__ == "__main__":
    print(f"With Python {sys.version}")
    sys.exit(main(sys.argv[1:]))
