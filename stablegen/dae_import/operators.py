"""Blender operators for DAE import and preprocessing."""

import os
import bpy  # pylint: disable=import-error

from ..utils import sg_modal_active


class ImportDAE(bpy.types.Operator):
    """Import and preprocess a COLLADA (.dae) file for StableGen texturing."""

    bl_idname = "stablegen.import_dae"
    bl_label = "Import DAE for Texturing"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(
        name="File Path",
        description="Path to the .dae file to import",
        subtype='FILE_PATH',
    )  # type: ignore

    directory: bpy.props.StringProperty(
        subtype='DIR_PATH',
    )  # type: ignore

    filter_glob: bpy.props.StringProperty(
        default="*.dae",
        options={'HIDDEN'},
    )  # type: ignore

    # ── Cleanup options ──

    merge_threshold: bpy.props.FloatProperty(
        name="Merge Distance",
        description=(
            "Distance below which vertices are considered coincident and merged. "
            "Default 1mm is safe for architectural models"
        ),
        default=0.001,
        min=0.0,
        max=0.1,
        precision=4,
        unit='LENGTH',
    )  # type: ignore

    remove_interior: bpy.props.BoolProperty(
        name="Remove Interior Faces",
        description="Detect and remove faces fully enclosed inside the mesh (internal walls/partitions)",
        default=True,
    )  # type: ignore

    strip_materials: bpy.props.BoolProperty(
        name="Replace Materials",
        description="Strip all imported materials and apply a clean Principled BSDF",
        default=True,
    )  # type: ignore

    # ── Topology options ──

    topology_method: bpy.props.EnumProperty(
        name="Topology",
        description="Method to improve mesh topology after cleanup",
        items=[
            ('NONE', "None", "Keep original topology. No changes"),
            ('FIX_FANS', "Fix Triangle Fans",
             "Only fixes fan patterns (many triangles radiating from one vertex) "
             "on flat surfaces. Keeps the rest of the topology intact. "
             "Also subdivides any remaining large faces"),
            ('SUBDIVIDE_ONLY', "Subdivide Large Faces",
             "Subdivide only faces above a threshold area. Keeps existing topology"),
            ('PLANAR_SUBDIVIDE', "Dissolve + Subdivide",
             "Merge coplanar triangles then retriangulate. "
             "WARNING: can create poor triangulation on large flat walls"),
            ('VOXEL', "Voxel Remesh",
             "Full remesh with uniform voxel size. Gives clean topology "
             "but loses sharp edges"),
        ],
        default='FIX_FANS',
    )  # type: ignore

    edge_ratio: bpy.props.FloatProperty(
        name="Edge Ratio",
        description=(
            "Edges longer than median length × this ratio get split. "
            "Lower = more uniform triangles but higher poly count"
        ),
        default=2.5,
        min=1.5,
        max=10.0,
        precision=1,
    )  # type: ignore

    equalize_iterations: bpy.props.IntProperty(
        name="Equalize Passes",
        description=(
            "Number of edge-splitting passes to equalize triangle density. "
            "More passes = more uniform but higher poly count"
        ),
        default=4,
        min=0,
        max=10,
    )  # type: ignore

    voxel_size: bpy.props.FloatProperty(
        name="Voxel Size",
        description="Voxel size for remesh (0 = auto-calculate from bounding box)",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=4,
        unit='LENGTH',
    )  # type: ignore

    # ── Scale / orientation ──

    auto_scale: bpy.props.FloatProperty(
        name="Target Size (BU)",
        description=(
            "Scale the imported model so its largest dimension equals this value "
            "in Blender Units. Set to 0 to keep the original scale"
        ),
        default=0.0,
        min=0.0,
        max=100.0,
    )  # type: ignore

    shading_mode: bpy.props.EnumProperty(
        name="Shading",
        description="Shading mode for the imported model",
        items=[
            ('FLAT', "Flat", "Flat shading (sharp edges, best for architecture)"),
            ('SMOOTH', "Smooth", "Smooth shading"),
            ('AUTO', "Auto Smooth", "Smooth shading with auto-smooth angle"),
        ],
        default='FLAT',
    )  # type: ignore

    center_origin: bpy.props.BoolProperty(
        name="Center at Origin",
        description="Move the imported model so its center is at the world origin",
        default=True,
    )  # type: ignore

    join_meshes: bpy.props.BoolProperty(
        name="Join Meshes",
        description="Join all imported mesh parts into a single object",
        default=True,
    )  # type: ignore

    triangulate: bpy.props.BoolProperty(
        name="Triangulate",
        description="Convert all faces to triangles (required for VR and best for UV projection)",
        default=True,
    )  # type: ignore

    @classmethod
    def poll(cls, context):
        return not sg_modal_active(context)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout

        layout.label(text="Cleanup", icon='BRUSH_DATA')
        box = layout.box()
        box.prop(self, "merge_threshold")
        box.prop(self, "remove_interior")
        box.prop(self, "strip_materials")
        box.prop(self, "join_meshes")
        box.prop(self, "triangulate")

        layout.separator()
        layout.label(text="Topology", icon='MOD_REMESH')
        box = layout.box()
        box.prop(self, "topology_method")
        if self.topology_method == 'FIX_FANS':
            box.prop(self, "edge_ratio")
            box.prop(self, "equalize_iterations")
        elif self.topology_method == 'VOXEL':
            box.prop(self, "voxel_size")

        layout.separator()
        layout.label(text="Transform", icon='OBJECT_ORIGIN')
        box = layout.box()
        box.prop(self, "auto_scale")
        box.prop(self, "shading_mode")
        box.prop(self, "center_origin")

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, "No valid .dae file selected")
            return {'CANCELLED'}

        import time
        t0 = time.monotonic()

        # ── Stage 1: Parse & Import ──
        from .parser import ColladaParser

        name_prefix = os.path.splitext(os.path.basename(self.filepath))[0]
        parser = ColladaParser(self.filepath)
        parser.parse()

        if not parser.instances:
            self.report({'WARNING'}, "No geometry found in the COLLADA file")
            return {'CANCELLED'}

        created = parser.create_blender_objects(context, name_prefix=name_prefix)
        if not created:
            self.report({'WARNING'}, "No mesh faces could be created from the file")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Imported {len(created)} mesh objects")

        # ── Stage 2: Remove edge-only objects ──
        from . import cleanup

        edge_removed = cleanup.remove_edge_only_objects(context)
        if edge_removed:
            self.report({'INFO'}, f"Removed {edge_removed} edge-only objects")

        # Refresh the created list (some may have been removed)
        created = [obj for obj in context.scene.objects
                   if obj.type == 'MESH' and obj.name.startswith(name_prefix)]

        if not created:
            self.report({'WARNING'}, "All imported geometry was edge-only (no faces)")
            return {'CANCELLED'}

        # ── Stage 3: Join meshes ──
        if self.join_meshes and len(created) > 1:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in created:
                obj.select_set(True)
            context.view_layer.objects.active = created[0]
            bpy.ops.object.join()
            created = [context.view_layer.objects.active]

        # ── Stage 3b: Center at origin (sit on floor) ──
        if self.center_origin:
            import mathutils as _mu  # pylint: disable=import-error
            for obj in created:
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                context.view_layer.objects.active = obj
                # Compute world-space bounding box
                corners = [obj.matrix_world @ _mu.Vector(c) for c in obj.bound_box]
                xs = [c.x for c in corners]
                ys = [c.y for c in corners]
                zs = [c.z for c in corners]
                # Center XY, put bottom at Z=0
                center_x = (min(xs) + max(xs)) / 2
                center_y = (min(ys) + max(ys)) / 2
                bottom_z = min(zs)
                obj.location.x -= center_x
                obj.location.y -= center_y
                obj.location.z -= bottom_z

        # ── Stage 4: Geometry cleanup ──
        for obj in created:
            # Merge coincident vertices
            if self.merge_threshold > 0:
                cleanup.merge_coincident_vertices(obj, self.merge_threshold)

            # Remove exact duplicate faces (same verts or same positions)
            dup_removed = cleanup.remove_exact_duplicate_faces(obj)
            if dup_removed:
                self.report({'INFO'}, f"Removed {dup_removed} duplicate faces from {obj.name}")

            # Remove coplanar overlapping faces (different tessellation, same surface)
            overlap_removed = cleanup.remove_coplanar_overlapping_faces(obj)
            if overlap_removed:
                self.report({'INFO'}, f"Removed {overlap_removed} coplanar overlaps from {obj.name}")

            # Remove loose geometry
            cleanup.remove_loose_geometry(obj)

            # Fix normals
            cleanup.fix_normals(obj)

            # Remove interior faces (internal walls/partitions)
            if self.remove_interior:
                int_removed = cleanup.remove_interior_faces(obj)
                if int_removed:
                    self.report({'INFO'}, f"Removed {int_removed} interior faces from {obj.name}")
                    # Clean up loose edges/verts left behind by face removal
                    cleanup.remove_loose_geometry(obj)

        # ── Stage 5: Topology improvement ──
        if self.topology_method != 'NONE':
            from . import topology

            for obj in created:
                if self.topology_method == 'FIX_FANS':
                    topology.fix_triangle_fans(
                        obj,
                        max_edge_ratio=self.edge_ratio,
                        equalize_iterations=self.equalize_iterations,
                    )
                elif self.topology_method == 'PLANAR_SUBDIVIDE':
                    topology.auto_improve_topology(obj, method='planar_subdivide')
                elif self.topology_method == 'SUBDIVIDE_ONLY':
                    topology.auto_improve_topology(obj, method='subdivide_only')
                elif self.topology_method == 'VOXEL':
                    topology.voxel_remesh(obj, voxel_size=self.voxel_size)

        # ── Stage 5b: Triangulate ──
        if self.triangulate and self.topology_method != 'FIX_FANS':
            # FIX_FANS already triangulates; skip to avoid double-triangulation
            for obj in created:
                bpy.context.view_layer.objects.active = obj
                obj.select_set(True)
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
                bpy.ops.object.mode_set(mode='OBJECT')

        # ── Stage 6: Materials ──
        if self.strip_materials:
            for obj in created:
                cleanup.strip_materials_and_apply_clean(obj)

        # ── Stage 7: Scale ──
        if self.auto_scale > 0:
            self._apply_auto_scale(context, created, self.auto_scale)

        # ── Stage 8: Shading ──
        if self.shading_mode == 'SMOOTH':
            for obj in created:
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.shade_smooth()
        elif self.shading_mode == 'AUTO':
            for obj in created:
                cleanup.auto_smooth_shading(obj)
        else:  # FLAT
            for obj in created:
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.shade_flat()

        # ── Stage 9: Apply transforms ──
        bpy.ops.object.select_all(action='DESELECT')
        for obj in created:
            obj.select_set(True)
        if created:
            context.view_layer.objects.active = created[0]
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        elapsed = time.monotonic() - t0
        verts = sum(len(o.data.vertices) for o in created)
        faces = sum(len(o.data.polygons) for o in created)
        self.report({'INFO'},
                    f"DAE import complete: {verts} verts, {faces} faces "
                    f"({elapsed:.1f}s)")

        return {'FINISHED'}

    @staticmethod
    def _apply_auto_scale(context, objects, target_bu):
        """Scale objects so the largest dimension equals target_bu."""
        import mathutils  # pylint: disable=import-error

        all_corners = []
        for obj in objects:
            for corner in obj.bound_box:
                all_corners.append(obj.matrix_world @ mathutils.Vector(corner))
        if not all_corners:
            return

        xs = [c.x for c in all_corners]
        ys = [c.y for c in all_corners]
        zs = [c.z for c in all_corners]
        extent = max(
            max(xs) - min(xs),
            max(ys) - min(ys),
            max(zs) - min(zs),
        )
        if extent < 1e-6:
            return

        scale_factor = target_bu / extent
        for obj in objects:
            obj.scale *= scale_factor

        # Apply scale
        bpy.ops.object.select_all(action='DESELECT')
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


class BatchImportDAE(bpy.types.Operator):
    """Batch import and convert all .dae files from a directory to .glb/.fbx.

    Clears the scene between each file, processes with the same cleanup/topology
    pipeline as single import, and exports the result. Supports recursive
    directory scanning — the output mirrors the source folder structure."""

    bl_idname = "stablegen.batch_import_dae"
    bl_label = "Batch Import DAE Files"
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(
        name="Directory",
        description="Directory containing .dae files",
        subtype='DIR_PATH',
    )  # type: ignore

    filter_glob: bpy.props.StringProperty(
        default="*.dae",
        options={'HIDDEN'},
    )  # type: ignore

    export_format: bpy.props.EnumProperty(
        name="Export Format",
        description="Format to export each processed model",
        items=[
            ('GLB', ".glb", "glTF Binary"),
            ('FBX', ".fbx", "FBX"),
        ],
        default='GLB',
    )  # type: ignore

    output_dir: bpy.props.StringProperty(
        name="Output Directory",
        description="Directory to save processed models (defaults to 'processed' subfolder). Type path manually",
        default="",
    )  # type: ignore

    recursive: bpy.props.BoolProperty(
        name="Recursive",
        description="Scan subdirectories recursively. Output mirrors the source folder structure",
        default=True,
    )  # type: ignore

    # ── Cleanup options ──

    merge_threshold: bpy.props.FloatProperty(
        name="Merge Distance",
        description=(
            "Distance below which vertices are considered coincident and merged. "
            "Default 1mm is safe for architectural models"
        ),
        default=0.001,
        min=0.0,
        max=0.1,
        precision=4,
        unit='LENGTH',
    )  # type: ignore

    remove_interior: bpy.props.BoolProperty(
        name="Remove Interior Faces",
        description="Detect and remove faces fully enclosed inside the mesh (internal walls/partitions)",
        default=True,
    )  # type: ignore

    strip_materials: bpy.props.BoolProperty(
        name="Replace Materials",
        description="Strip all imported materials and apply a clean Principled BSDF",
        default=True,
    )  # type: ignore

    triangulate: bpy.props.BoolProperty(
        name="Triangulate",
        description="Convert all faces to triangles",
        default=True,
    )  # type: ignore

    # ── Topology options ──

    topology_method: bpy.props.EnumProperty(
        name="Topology",
        description="Method to improve mesh topology after cleanup",
        items=[
            ('NONE', "None", "Keep original topology"),
            ('FIX_FANS', "Fix Triangle Fans",
             "Only fixes fan patterns on flat surfaces. "
             "Also subdivides any remaining large faces"),
            ('SUBDIVIDE_ONLY', "Subdivide Large Faces",
             "Subdivide only faces above a threshold area"),
            ('PLANAR_SUBDIVIDE', "Dissolve + Subdivide",
             "Merge coplanar triangles then retriangulate"),
            ('VOXEL', "Voxel Remesh", "Full remesh with uniform voxel size"),
        ],
        default='FIX_FANS',
    )  # type: ignore

    edge_ratio: bpy.props.FloatProperty(
        name="Edge Ratio",
        description="Edges longer than median × this ratio get split",
        default=2.5,
        min=1.5,
        max=10.0,
        precision=1,
    )  # type: ignore

    equalize_iterations: bpy.props.IntProperty(
        name="Equalize Passes",
        description="Number of edge-splitting passes to equalize triangle density",
        default=4,
        min=0,
        max=10,
    )  # type: ignore

    voxel_size: bpy.props.FloatProperty(
        name="Voxel Size",
        description="Voxel size for remesh (0 = auto-calculate)",
        default=0.0,
        min=0.0,
        max=1.0,
        precision=4,
        unit='LENGTH',
    )  # type: ignore

    # ── Scale / orientation ──

    auto_scale: bpy.props.FloatProperty(
        name="Target Size (BU)",
        description="Scale so largest dimension equals this value. 0 = keep original",
        default=0.0,
        min=0.0,
        max=100.0,
    )  # type: ignore

    shading_mode: bpy.props.EnumProperty(
        name="Shading",
        items=[
            ('FLAT', "Flat", "Flat shading"),
            ('SMOOTH', "Smooth", "Smooth shading"),
            ('AUTO', "Auto Smooth", "Smooth with auto-smooth angle"),
        ],
        default='FLAT',
    )  # type: ignore

    center_origin: bpy.props.BoolProperty(
        name="Center at Origin",
        description="Move each model so its center is at the world origin",
        default=True,
    )  # type: ignore

    @classmethod
    def poll(cls, context):
        return not sg_modal_active(context)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout

        layout.label(text="Export", icon='EXPORT')
        box = layout.box()
        box.prop(self, "export_format")
        box.prop(self, "output_dir")
        box.prop(self, "recursive")

        layout.separator()
        layout.label(text="Cleanup", icon='BRUSH_DATA')
        box = layout.box()
        box.prop(self, "merge_threshold")
        box.prop(self, "remove_interior")
        box.prop(self, "strip_materials")
        box.prop(self, "triangulate")

        layout.separator()
        layout.label(text="Topology", icon='MOD_REMESH')
        box = layout.box()
        box.prop(self, "topology_method")
        if self.topology_method == 'FIX_FANS':
            box.prop(self, "edge_ratio")
            box.prop(self, "equalize_iterations")
        elif self.topology_method == 'VOXEL':
            box.prop(self, "voxel_size")

        layout.separator()
        layout.label(text="Transform", icon='OBJECT_ORIGIN')
        box = layout.box()
        box.prop(self, "auto_scale")
        box.prop(self, "shading_mode")
        box.prop(self, "center_origin")

    def _collect_dae_files(self):
        """Return list of (absolute_path, relative_path) tuples for .dae files."""
        root = self.directory.rstrip('/\\')
        results = []
        if self.recursive:
            for dirpath, _dirs, files in os.walk(root):
                for f in sorted(files):
                    if f.lower().endswith('.dae'):
                        abs_path = os.path.join(dirpath, f)
                        rel_path = os.path.relpath(abs_path, root)
                        results.append((abs_path, rel_path))
        else:
            for f in sorted(os.listdir(root)):
                if f.lower().endswith('.dae'):
                    results.append((os.path.join(root, f), f))
        return results

    def execute(self, context):
        if not self.directory or not os.path.isdir(self.directory):
            self.report({'ERROR'}, "No valid directory selected")
            return {'CANCELLED'}

        dae_files = self._collect_dae_files()
        if not dae_files:
            self.report({'WARNING'}, "No .dae files found")
            return {'CANCELLED'}

        out_dir = self.output_dir or os.path.join(
            self.directory.rstrip('/\\'), "processed")
        os.makedirs(out_dir, exist_ok=True)

        import time
        t0 = time.monotonic()
        processed = 0
        failed = []
        total = len(dae_files)

        wm = context.window_manager
        wm.progress_begin(0, total)

        for i, (filepath, rel_path) in enumerate(dae_files):
            name = os.path.splitext(os.path.basename(filepath))[0]
            print(f"[BatchDAE] {i + 1}/{total}: {rel_path}")
            wm.progress_update(i)

            # Clear scene
            bpy.ops.object.select_all(action='SELECT')
            bpy.ops.object.delete(use_global=False)

            try:
                bpy.ops.stablegen.import_dae(
                    filepath=filepath,
                    merge_threshold=self.merge_threshold,
                    remove_interior=self.remove_interior,
                    strip_materials=self.strip_materials,
                    topology_method=self.topology_method,
                    edge_ratio=self.edge_ratio,
                    equalize_iterations=self.equalize_iterations,
                    voxel_size=self.voxel_size,
                    auto_scale=self.auto_scale,
                    shading_mode=self.shading_mode,
                    center_origin=self.center_origin,
                    join_meshes=True,
                    triangulate=self.triangulate,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[BatchDAE] FAILED to import {rel_path}: {exc}")
                failed.append(rel_path)
                continue

            # Build output path mirroring directory structure
            rel_dir = os.path.dirname(rel_path)
            file_out_dir = os.path.join(out_dir, rel_dir) if rel_dir else out_dir
            os.makedirs(file_out_dir, exist_ok=True)

            try:
                if self.export_format == 'GLB':
                    out_path = os.path.join(file_out_dir, f"{name}.glb")
                    bpy.ops.export_scene.gltf(filepath=out_path)
                elif self.export_format == 'FBX':
                    out_path = os.path.join(file_out_dir, f"{name}.fbx")
                    bpy.ops.export_scene.fbx(filepath=out_path)
            except Exception as exc:  # noqa: BLE001
                print(f"[BatchDAE] FAILED to export {rel_path}: {exc}")
                failed.append(rel_path)
                continue

            processed += 1

        elapsed = time.monotonic() - t0
        wm.progress_end()
        msg = f"Batch: {processed}/{total} files in {elapsed:.1f}s"
        if failed:
            msg += f" ({len(failed)} failed)"
            for f in failed:
                print(f"[BatchDAE] Failed: {f}")
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# Classes to register with Blender
DAE_IMPORT_CLASSES = [
    ImportDAE,
    BatchImportDAE,
]
